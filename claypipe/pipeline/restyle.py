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
    ledger: "object | None" = None,
    scorer: "object | None" = None,
    controller: "object | None" = None,
    references: "list | None" = None,
    scores_path: Path | None = None,
) -> int:
    """Restyle every source frame, scoring and retrying when a scorer is given.

    Resume-safe: an existing output is never re-generated, so a crashed run
    never re-spends (SPEC §2).

    `seed_for` maps a 1-based frame index to a seed. Step 1 has no shot
    detection yet, so the default is the whole clip as shot 0 (seed 1000).
    Shot-aware seeding (seed = 1000 + shot_index) lands with shots.py.

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

            score = _score_and_record(
                scorer=scorer, frame=src.name, source=src, restyled=dst,
                references=references or [], previous=previous_accepted,
                scores_path=scores_path,
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
        est_cost_usd=round(done * backend.cost_per_frame_usd(), 4),
    )
    return len(frames)


def _score_and_record(
    *, scorer, frame: str, source: Path, restyled: Path,
    references: list, previous: Path | None, scores_path: Path | None,
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
        with scores_path.open("a") as fh:
            fh.write(json.dumps(score.to_dict()) + "\n")
    return score
