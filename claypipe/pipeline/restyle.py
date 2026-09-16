"""Stage: AI restyle (SPEC §2).

The defining architectural rule: frames are restyled as IMAGES, one at a time.
No video model, ever — timing and audio are never regenerated.

Backends sit behind the `RestyleBackend` protocol so a real endpoint (fal.ai
Flux Kontext or SDXL+ControlNet — SPEC A2, deferred) can be dropped in without
the pipeline changing. Step 1 ships only DummyBackend: offline, deterministic,
zero API spend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image, ImageEnhance, ImageFilter

from ..config import BILLED_FRAMES_PER_SECOND
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


@runtime_checkable
class ClipRestyleBackend(Protocol):
    """One restyled CLIP RANGE per call — the Track C seam (T13/A4).

    Deliberately a SEPARATE protocol rather than a generalisation of
    `RestyleBackend`. The two look similar and behave nothing alike:

      * RETRY. A failed frame reseeds one frame. A failed chunk reseeds 81-240
        frames at once, so one Track C retry can cost more than ten Track A
        retries. Collapsing them into one protocol would mean one retry cap and
        one budget fraction for two units of wildly different price.
      * TIMING. A per-frame backend cannot change the frame count. A clip
        backend works in chunks at its own frame rate (VACE: 81-240 frames at
        16fps), which is why the frame-count invariant becomes a DURATION
        invariant plus the unchanged audio hash (A2/T17).
      * FAILURE GRANULARITY. A frame either exists or does not. A chunk can
        come back the wrong length, which is a distinct failure with no
        per-frame analogue.

    Generalising one into the other would hide all three differences behind a
    shared signature. Two protocols, one seam.
    """

    name: str
    # Chunk bounds the backend can actually honour. VACE is 81-240 at 16fps.
    min_chunk_frames: int
    max_chunk_frames: int
    native_fps: int

    def cost_per_video_second_usd(self) -> float:
        """Estimated spend per video-second. Drives the cost firewalls."""

    def restyle_clip(
        self,
        src_frames: "list[Path]",
        out_dir: Path,
        *,
        prompt: str,
        strength: float,
        seed: int,
    ) -> "list[Path]":
        """Restyle a contiguous range of frames. Returns what it wrote.

        The returned list is CHECKED against the input length by the caller: a
        chunk that comes back short would otherwise shorten the clip and break
        the duration invariant silently.
        """


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


class DummyClipBackend:
    """Offline, free, deterministic ClipRestyleBackend. Zero API spend.

    Same role DummyBackend plays for Track A: it makes the ENTIRE Track C path
    runnable and testable with no credential and no money, including the parts
    that only a clip backend has — chunking, the native-fps mismatch, and a
    chunk coming back the wrong length.

    It deliberately imitates VACE's awkward properties rather than being
    convenient: it works at 16fps natively against the pipeline's 12, and it
    accepts 81-240 frames per chunk. Those are the two facts that force the
    duration invariant (A2/T17), so a stand-in that ignored them would let the
    pipeline pass tests it should fail.
    """

    name = "dummy_clip"
    # Mirrors Wan VACE's real schema, verified on fal 2026-09-15: num_frames
    # "must be between 81 to 241 (inclusive)", and "video seconds are
    # calculated at 16 frames per second".
    min_chunk_frames = 81
    max_chunk_frames = 241
    native_fps = 16

    def cost_per_video_second_usd(self) -> float:
        return 0.0

    def restyle_clip(
        self,
        src_frames: list[Path],
        out_dir: Path,
        *,
        prompt: str,
        strength: float,
        seed: int,
    ) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for offset, src in enumerate(src_frames):
            dst = out_dir / src.name
            with Image.open(src) as img:
                frame = img.convert("RGB")
            # A resynthesis stand-in must MOVE GEOMETRY, or it would exercise
            # the surface-mode gate instead of the resynth one. A small
            # per-frame shift plus a posterise does that deterministically.
            shift = (seed + offset) % 5 - 2
            moved = frame.transform(
                frame.size, Image.AFFINE, (1, 0, shift, 0, 1, shift),
                resample=Image.BILINEAR,
            )
            levels = max(2, 8 - int(round(strength * 5)))
            styled = moved.filter(ImageFilter.SMOOTH_MORE)
            styled = Image.eval(
                styled, lambda v, n=levels: int(v / (256 / n)) * (255 // (n - 1))
            )
            styled.save(dst)
            written.append(dst)
        return written


def get_backend(name: str, *, live: bool = False) -> RestyleBackend:
    if name == "dummy":
        return DummyBackend()
    if name == "fal":
        return FalBackend(live=live)
    raise ValueError(f"unknown backend {name!r} (available: dummy, fal)")


def get_clip_backend(name: str, *, live: bool = False) -> ClipRestyleBackend:
    """Track C backends. Separate resolver, because the protocols are separate
    (A4) and a caller that wants a clip backend must not silently receive a
    per-frame one."""
    if name in ("dummy", "dummy_clip"):
        return DummyClipBackend()
    raise ValueError(
        f"unknown clip backend {name!r} (available: dummy_clip). Wan VACE and "
        "Runway Aleph land with T16/T19 — no Track C endpoint is wired yet."
    )


class ChunkLengthError(RuntimeError):
    """A clip backend returned a different number of frames than it was given.

    This failure has NO per-frame analogue, which is the reason
    ClipRestyleBackend is a separate protocol. A short chunk would shorten the
    clip, break the duration invariant, and desync the audio — in a file that
    plays.
    """


def restyle_clip_range(
    *,
    backend: ClipRestyleBackend,
    src_frames: list[Path],
    out_dir: Path,
    prompt: str,
    strength: float,
    seed: int,
    logger: RunLogger,
    ledger: "object | None" = None,
    fps: int,
) -> list[Path]:
    """Restyle a contiguous frame range in chunks the backend can honour.

    The returned length is CHECKED against the input length, per chunk. A
    backend that returns 80 frames for 81 would otherwise shorten the clip and
    desync the audio, and the resulting file would play perfectly.

    Chunks are authorised against the ledger in FRAME COUNT (V1), because that
    is what fal's Wan VACE actually bills: "Video seconds are calculated at 16
    frames per second". Passing a wall-clock duration instead would misprice
    every call by the ratio between our 12fps cadence and 16 — a 2x error the
    price model now refuses outright rather than absorbing.
    """
    if not src_frames:
        raise FileNotFoundError("restyle_clip_range got no frames")
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    total = len(src_frames)
    start = 0
    chunk_index = 0

    while start < total:
        remaining = total - start
        size = min(backend.max_chunk_frames, remaining)
        # Never emit a chunk below the backend's floor unless it is the whole
        # remainder — a backend asked for fewer frames than it supports may
        # pad, which would change the frame count.
        if size < backend.min_chunk_frames and remaining == size and written:
            # Fold the short tail into the previous chunk's re-run instead of
            # sending an undersized request.
            start = max(0, start - (backend.min_chunk_frames - size))
            size = min(backend.max_chunk_frames, total - start)
        chunk = src_frames[start : start + size]

        already = [out_dir / f.name for f in chunk]
        if all(p.is_file() for p in already):
            logger.info(
                "restyle.clip.skip", chunk=chunk_index, frames=len(chunk),
                reason="already restyled",
            )
            written.extend(already)
            start += size
            chunk_index += 1
            continue

        entry_id = None
        if ledger is not None:
            entry_id = ledger.authorize(
                frame=f"clip_{chunk[0].stem}-{chunk[-1].stem}",
                backend=backend.name, stage="batch", frames=len(chunk),
            )
        produced = backend.restyle_clip(
            chunk, out_dir, prompt=prompt, strength=strength, seed=seed + chunk_index
        )
        if ledger is not None and entry_id is not None:
            ledger.reconcile(
                entry_id,
                backend.cost_per_video_second_usd()
                * len(chunk)
                / BILLED_FRAMES_PER_SECOND,
            )

        if len(produced) != len(chunk):
            raise ChunkLengthError(
                f"clip backend {backend.name!r} was given {len(chunk)} frames "
                f"(chunk {chunk_index}: {chunk[0].name}..{chunk[-1].name}) and "
                f"returned {len(produced)}. A chunk of the wrong length "
                "shortens the clip and desyncs the audio, in a file that plays. "
                "Refusing to continue."
            )
        logger.info(
            "restyle.clip.chunk", chunk=chunk_index, frames=len(chunk),
            billed_seconds=round(len(chunk) / BILLED_FRAMES_PER_SECOND, 4),
            timeline_seconds=round(len(chunk) / fps, 4),
            seed=seed + chunk_index,
            native_fps=backend.native_fps, pipeline_fps=fps,
        )
        written.extend(produced)
        start += size
        chunk_index += 1

    logger.info(
        "restyle.clip",
        backend=backend.name, frames=len(written), chunks=chunk_index,
        est_cost_usd=round(
            backend.cost_per_video_second_usd()
            * len(written)
            / BILLED_FRAMES_PER_SECOND,
            4,
        ),
    )
    return written


def restyle_frames(
    *,
    backend: RestyleBackend,
    source_dir: Path,
    out_dir: Path,
    prompt: str,
    strength: float,
    logger: RunLogger,
    seed_for: "callable[[int], int] | None" = None,
    ledger: "object | None" = None,
    scorer: "object | None" = None,
    controller: "object | None" = None,
    references: "list | None" = None,
    scores_path: Path | None = None,
    boundary_frames: "set[int] | None" = None,
) -> int:
    """Restyle every source frame, scoring and retrying when a scorer is given.

    Resume-safe: an existing output is never re-generated, so a crashed run
    never re-spends (SPEC §2).

    `seed_for` maps a 1-based frame index to a seed. With a shot plan this is
    `ShotPlan.seed_for_frame`, giving one fixed seed per shot; without one it
    defaults to the whole clip as shot 0 (seed 1000).

    `boundary_frames` (T11) is the set of 1-based indices that OPEN a shot. At
    one of those the previous restyled frame belongs to a different scene, so
    it is not handed to the scorer: a flow-warped residual across a hard cut
    measures nothing and reads as catastrophic temporal failure. The frame is
    scored as a first frame instead, which is exactly what it is.

    SCORING IS DELIBERATELY OPTIONAL, and is skipped for DummyBackend
    (memory.md D27). The dummy's entire visual difference from the source is a
    posterise plus `saturation = 1.0 + (seed % 7) * 0.05` — a deterministic
    palette nudge with no generative content at all. Scoring it would produce
    numbers that look like quality measurements while measuring nothing, and
    would burn LPIPS and CLIP inference on every frame of every offline test
    run to do it. An unscored offline run says so; it does not fake a score.
    """
    frames = frame_paths(source_dir)
    if not frames:
        raise FileNotFoundError(f"no source frames in {source_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    seed_for = seed_for or (lambda _idx: 1000)

    done = 0
    skipped = 0
    retried = 0
    boundaries = boundary_frames or set()
    previous_accepted: Path | None = None

    for idx, src in enumerate(frames, start=1):
        dst = out_dir / src.name
        if dst.is_file():
            skipped += 1
            previous_accepted = dst
            continue

        attempt_strength = strength
        attempt_seed = seed_for(idx)

        while True:
            # Every call is authorised and written to the spend ledger BEFORE
            # it executes; a cap breach raises out of here, untried and unpaid.
            entry_id = None
            if ledger is not None:
                entry_id = ledger.authorize(
                    frame=src.name, backend=backend.name, stage="batch"
                )
            backend.restyle(
                src, dst, prompt=prompt, strength=attempt_strength, seed=attempt_seed
            )
            if ledger is not None and entry_id is not None:
                ledger.reconcile(entry_id, backend.cost_per_frame_usd())
            done += 1

            if scorer is None or controller is None:
                break

            # A shot-opening frame has no comparable predecessor. See the
            # boundary_frames note above — this is the D15/D34 fix.
            predecessor = None if idx in boundaries else previous_accepted
            score = _score_and_record(
                scorer=scorer, frame=src.name, source=src, restyled=dst,
                references=references or [], previous=predecessor,
                scores_path=scores_path, shot_boundary=idx in boundaries,
            )
            controller.observe(score)
            decision = controller.decide(score, base_seed=seed_for(idx))

            if not decision.is_retry:
                logger.info(
                    "restyle.frame.settled", frame=src.name, f=round(score.f, 4),
                    verdict=score.verdict.value, reason=score.reason.value,
                    action=decision.action.value,
                )
                break

            retried += 1
            # D18 = A: a missed component target retries at a LOWER STRENGTH
            # with the shot's seed retained; only a composite FAIL reseeds.
            if decision.strength is not None:
                attempt_strength = decision.strength
            if decision.seed is not None:
                attempt_seed = decision.seed
            logger.info(
                "restyle.frame.retry", frame=src.name, attempt=decision.attempt,
                reason=score.reason.value, strength=attempt_strength, seed=attempt_seed,
            )

        previous_accepted = dst

    logger.info(
        "restyle.frames",
        backend=backend.name,
        restyled=done,
        resumed=skipped,
        retried=retried,
        total=len(frames),
        strength=strength,
        scored=scorer is not None,
        shot_boundaries=len(boundaries),
        est_cost_usd=round(done * backend.cost_per_frame_usd(), 4),
    )
    return len(frames)


def _score_and_record(
    *, scorer, frame: str, source: Path, restyled: Path,
    references: list, previous: Path | None, scores_path: Path | None,
    shot_boundary: bool = False,
):
    """Score one frame and append it to scores.jsonl (SPEC §3)."""
    from .score import load_image

    score = scorer.score_frame(
        frame=frame,
        source=load_image(source),
        restyled=load_image(restyled),
        references=references,
        previous_restyled=load_image(previous) if previous is not None else None,
    )
    if scores_path is not None:
        scores_path.parent.mkdir(parents=True, exist_ok=True)
        record = score.to_dict()
        # Recorded so a reviewer reading scores.jsonl can see WHY a frame's TF
        # is the first-frame value rather than a measurement (T11).
        record["shot_boundary"] = shot_boundary
        with scores_path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    return score

# --------------------------------------------------------------------------
# The paid backend. Stubbed: no network call exists in this file yet.
# --------------------------------------------------------------------------

class FalCallError(RuntimeError):
    """A fal.ai call failed. Carries the response body for the incident note."""

    def __init__(self, message: str, *, response_body: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.response_body = response_body
        self.status = status


@runtime_checkable
class FalClientLike(Protocol):
    """The seam the real client plugs into, and the tests stand in for.

    Kept deliberately narrow — one method — so a stub is trivially faithful and
    so nothing in the pipeline can reach past it to the network.
    """

    def edit_image(
        self, *, image_bytes: bytes, prompt: str, strength: float, seed: int, model: str
    ) -> bytes:
        """Return restyled PNG bytes, or raise FalCallError."""


class FalBackend:
    """fal.ai image edit (Flux Kontext), operator-pinned over SDXL+ControlNet.

    NO NETWORK CODE LIVES HERE YET. `restyle()` refuses unless it has been given
    a client, and the only client that exists today is the test stub. The real
    one lands with the first live canary, which is a manual operator step by
    design — a first paid call should be watched by a human, not discovered in
    a log.

    The cost estimate is read from config and is a CEILING, not a guess: a call
    that would cost more than the configured estimate is refused rather than
    quietly charged.
    """

    name = "fal"

    def __init__(
        self,
        *,
        live: bool = False,
        client: "FalClientLike | None" = None,
        cfg: "object | None" = None,
        model: str | None = None,
    ) -> None:
        self.live = live
        self.client = client
        self._cfg = cfg
        self._model = model
        self.planned_calls: list[dict] = []

    # -- config ----------------------------------------------------------
    def _firewalls(self):
        if self._cfg is not None:
            return self._cfg
        from ..config import load_weights

        self._cfg = load_weights().firewalls
        return self._cfg

    def cost_per_frame_usd(self) -> float:
        from ..config import ConfigError

        prices = self._firewalls().cost.estimated_usd_per_call
        if "fal" not in prices:
            raise ConfigError(
                "firewalls.cost.estimated_usd_per_call.fal is required when "
                "--backend fal. Refusing to spend against an unknown price."
            )
        return prices["fal"]

    def model_name(self) -> str:
        if self._model:
            return self._model
        models = getattr(self._firewalls(), "fal", None)
        return getattr(models, "model", None) or DEFAULT_FAL_MODEL

    def assert_affordable(self, quoted_usd: float) -> None:
        """The configured estimate is a CEILING on what a call may cost.

        If the endpoint quotes above it, that is a pricing change nobody
        approved, and the run stops rather than absorbing it silently.
        """
        ceiling = self.cost_per_frame_usd()
        if quoted_usd > ceiling:
            raise FalCallError(
                f"endpoint quoted ${quoted_usd:.4f} per call, above the "
                f"configured ceiling of ${ceiling:.4f} "
                "(firewalls.cost.estimated_usd_per_call.fal). Refusing: a price "
                "rise is a decision, not a rounding error."
            )

    # -- restyle ---------------------------------------------------------
    def restyle(self, src: Path, dst: Path, *, prompt: str, strength: float, seed: int) -> None:
        if not self.live or self.client is None:
            raise NotImplementedError(
                "FalBackend requires --live and FAL_KEY and a stubbed or real "
                "client; see T7. No network code exists in this backend yet."
            )
        payload = self.client.edit_image(
            image_bytes=src.read_bytes(), prompt=prompt, strength=strength,
            seed=seed, model=self.model_name(),
        )
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(payload)

    def plan(self, src: Path, *, prompt: str, strength: float, seed: int) -> dict:
        """Dry run: what WOULD be called, and what it would be charged.

        Records the intent without making it, so the ledger wiring can be
        proven end to end with no network and no spend.
        """
        entry = {
            "frame": src.name,
            "model": self.model_name(),
            "strength": strength,
            "seed": seed,
            "estimated_usd": self.cost_per_frame_usd(),
            "prompt_chars": len(prompt),
        }
        self.planned_calls.append(entry)
        return entry


DEFAULT_FAL_MODEL = "fal-ai/flux-pro/kontext"
