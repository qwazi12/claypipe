"""Adaptive keyframe propagation (MASTER_PLAN T18/§1.3).

The cost argument, measured: the denser reference clip runs 28.9 shots per 60
seconds, about 24 frames per shot at 12fps. Restyling every frame of a 60s clip
is 720 paid generations. Restyling one frame per shot and WARPING the rest
forward along optical flow is ~30-60 — a 12-24x reduction, not the 6x the
SPEC's EbSynth note assumes.

THE KEY DESIGN QUESTION, and it is easy to get wrong: how do you decide a warp
was good enough, when you never generated the frame you would be comparing
against?

You cannot compare the warped restyled frame to "the real restyled frame" —
not generating it is the entire point. So the decision is made in the SOURCE
domain: warp source[i-1] along the flow and compare it to the actual source[i].
That measures how well flow explains the motion that really happened. If flow
explains the source well, the same warp applied to the restyled frame is
trustworthy; where flow fails — occlusion, a fast pan, a limb crossing a body —
it fails in both domains, and that frame becomes a new paid keyframe.

This is why T11 comes first. A shot boundary makes flow meaningless: the
previous frame is a different scene entirely, and a warp across it produces
garbage with a plausible-looking residual. Every shot therefore OPENS with a
paid keyframe, unconditionally.

Reuses `score.motion_compensated_residual`'s flow configuration, so the thing
deciding a keyframe and the thing grading temporal fidelity cannot disagree
about what motion is.

MEASURED ON A REAL 60-SECOND REFERENCE SLICE (720 frames, 28 shots), and two
findings contradict T18's own premises. Both are recorded here because acting
on the premises instead of the measurement would have shipped a worse default.

FINDING 1 — TEMPORAL FIDELITY CANNOT VALIDATE PROPAGATION. T18's acceptance
criterion is "propagated frames score TF >= 0.99". TF measured:

    max_chain   paid   reduction   TF min   TF mean   TF p5    % >= 0.99
            6    142       5.07x   0.8833    0.9905  0.9587        75.4%
           12     96        7.5x   0.8784    0.9921  0.9676        79.0%
           24     74       9.73x   0.8951    0.9932  0.9755        83.0%
     unbounded    59       12.2x   0.9216    0.9947  0.9814        84.4%

TF gets BETTER as chains get LONGER — the exact opposite of the drift
intuition `max_chain` was built on. The reason is structural: a warped frame is
by construction a smooth resampling of its predecessor, so it is almost
perfectly temporally consistent. A KEYFRAME is a fresh generation that does not
match its predecessor, so every keyframe INSERTION is a temporal
discontinuity. Fewer keyframes therefore means better TF.

So TF does not measure propagation drift at all; it measures how often we
interrupt a propagation chain. The criterion is not met (84.4% of propagated
frames reach 0.99 at best, mean 0.9947, min 0.92) and would not become
meaningful by tightening it. What catches smear is fidelity TO THE SOURCE —
SSIM and LPIPS-edges — not frame-to-frame consistency.

FINDING 2 — SSIM-VS-SOURCE IS FLAT IN CHAIN DEPTH ON THIS FOOTAGE.

    warp depth      n    SSIM mean   SSIM min
        keyframe   59       0.3671     0.2939
             0-4  125       0.3493     0.2380
            5-9   142       0.3341     0.1940
          10-14   101       0.3318     0.1862
          20-24    50       0.3433     0.1880
          40-44   101       0.3146     0.1835

A 40-warp chain loses about 0.05 SSIM against its own source, and a keyframe
only starts at 0.367. So `max_chain` earns very little here.

WHY THE DEFAULT IS STILL CONSERVATIVE (12, not unbounded): both measurements
were taken with DummyBackend, whose output is flat posterised colour fields.
Resampling a flat field is nearly lossless, so the dummy CANNOT exhibit the
drift that repeated resampling would cause in a real restyle's clay texture,
fingerprints and tool marks. The dummy's evidence that long chains are safe is
therefore weak exactly where it matters. max_chain=12 still buys 7.5x
($3.84/clip instead of $28.80 on Kontext pro); `--keyframe-max-chain` raises it,
and 12.2x / 59 paid frames is where the plan's "<= 60" target actually lands.
Re-measure at T16 against a real backend before changing the default.

NOTE ON THE ABSOLUTE SSIM NUMBERS: 0.33-0.37 against a target of 0.72 is not a
propagation failure — it is the DUMMY BACKEND scoring against real footage. The
same posterise scores above 0.72 on the synthetic fixtures. That is finding F4
again, and memory.md D48: fixture-calibrated thresholds do not transfer to real
frames.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import TemporalConfig
from ..logging import RunLogger
from .shots import ShotPlan

# A frame whose flow-explained residual exceeds this (0-1, normalised) gets a
# new paid keyframe instead of a warp.
#
# Measured on the 60s reference slice: this threshold is what does the real
# work. Sweeping it moved paid frames from 177 (at 0.03) to 85 (at 0.07), after
# which it saturates — above ~0.07 essentially every frame's flow is "good
# enough" and only shot opens and the chain limit produce keyframes. 0.055 sits
# on the knee.
DEFAULT_RESIDUAL_MAX = 0.055

# Hard ceiling on chained warps, independent of residual.
#
# The measurement in the module docstring says this earns little on the dummy
# backend — but the dummy's flat posterised fields resample almost losslessly,
# so it cannot show the smear that repeated resampling would cause in real clay
# texture. Kept conservative on that basis, not on the measurement. 12 buys
# 7.5x; `--keyframe-max-chain` raises it, and unbounded is 12.2x.
DEFAULT_MAX_CHAIN = 12


class PropagationError(RuntimeError):
    """Propagation could not run, or produced an unusable plan."""


@dataclass(frozen=True)
class FramePlan:
    """What happens to one frame: paid generation, or a warp from its neighbour."""

    index: int
    shot_index: int
    is_keyframe: bool
    reason: str
    residual: float | None = None
    chain_length: int = 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "shot_index": self.shot_index,
            "keyframe": self.is_keyframe,
            "reason": self.reason,
            "residual": None if self.residual is None else round(self.residual, 5),
            "chain_length": self.chain_length,
        }


@dataclass(frozen=True)
class KeyframePlan:
    """Which frames get paid for, and which get warped."""

    total_frames: int
    frames: list[FramePlan]
    residual_max: float
    max_chain: int

    @property
    def keyframes(self) -> list[int]:
        return [f.index for f in self.frames if f.is_keyframe]

    @property
    def paid_frames(self) -> int:
        return len(self.keyframes)

    @property
    def propagated_frames(self) -> int:
        return self.total_frames - self.paid_frames

    @property
    def reduction_factor(self) -> float:
        return self.total_frames / self.paid_frames if self.paid_frames else 0.0

    def is_keyframe(self, index: int) -> bool:
        return any(f.index == index and f.is_keyframe for f in self.frames)

    def summary(self) -> dict:
        by_reason: dict[str, int] = {}
        for frame in self.frames:
            if frame.is_keyframe:
                by_reason[frame.reason] = by_reason.get(frame.reason, 0) + 1
        return {
            "total_frames": self.total_frames,
            "paid_frames": self.paid_frames,
            "propagated_frames": self.propagated_frames,
            "reduction_factor": round(self.reduction_factor, 2),
            "keyframe_reasons": by_reason,
            "residual_max": self.residual_max,
            "max_chain": self.max_chain,
        }

    def as_dict(self) -> dict:
        return {"schema_version": 1, **self.summary(), "frame_plan": [f.as_dict() for f in self.frames]}

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2) + "\n")
        tmp.replace(path)
        return path

    @classmethod
    def read(cls, path: Path) -> "KeyframePlan":
        data = json.loads(path.read_text())
        return cls(
            total_frames=data["total_frames"],
            residual_max=data["residual_max"],
            max_chain=data["max_chain"],
            frames=[
                FramePlan(
                    index=f["index"], shot_index=f["shot_index"],
                    is_keyframe=f["keyframe"], reason=f["reason"],
                    residual=f.get("residual"), chain_length=f.get("chain_length", 0),
                )
                for f in data["frame_plan"]
            ],
        )


def _gray(path: Path):
    import numpy as np
    from PIL import Image

    with Image.open(path) as img:
        return np.asarray(img.convert("L"), dtype=np.uint8)


def flow_between(previous, current, cfg: TemporalConfig):
    """Farneback flow, with the SAME configuration temporal fidelity uses.

    Shared deliberately: the thing deciding a keyframe and the thing grading
    temporal fidelity must not disagree about what motion is.
    """
    import cv2

    return cv2.calcOpticalFlowFarneback(
        previous, current, None,
        cfg.pyr_scale, cfg.levels, cfg.winsize, cfg.iterations,
        cfg.poly_n, cfg.poly_sigma, 0,
    )


def warp(image, flow):
    """Push `image` along `flow`. Works on grayscale or colour."""
    import cv2
    import numpy as np

    h, w = flow.shape[:2]
    grid_x, grid_y = np.meshgrid(
        np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32)
    )
    map_x = (grid_x + flow[..., 0]).astype(np.float32)
    map_y = (grid_y + flow[..., 1]).astype(np.float32)
    return cv2.remap(
        image, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )


def flow_residual(source_previous, source_current, cfg: TemporalConfig) -> float:
    """How well flow explains the motion that ACTUALLY happened, 0-1.

    Measured in the SOURCE domain, which is the whole trick. The restyled frame
    we would want to compare against does not exist — not generating it is the
    point of propagation. But where flow fails it fails in both domains, so the
    source-domain residual is a valid proxy, and it is available for free
    before any money is spent.
    """
    import numpy as np

    flow = flow_between(source_previous, source_current, cfg)
    warped = warp(source_previous, flow)
    residual = np.abs(warped.astype(np.float32) - source_current.astype(np.float32))
    return float(residual.mean() / 255.0)


def plan_keyframes(
    *,
    source_frames: list[Path],
    shot_plan: ShotPlan,
    cfg: TemporalConfig,
    residual_max: float = DEFAULT_RESIDUAL_MAX,
    max_chain: int = DEFAULT_MAX_CHAIN,
    logger: RunLogger | None = None,
) -> KeyframePlan:
    """Decide which frames are paid for. No money is spent here.

    Planning is deliberately separate from execution, and runs BEFORE the
    ledger authorises anything: the operator can see the paid-frame count and
    the projected cost before a single call goes out. A propagation scheme that
    only reveals its cost by spending it is not a cost reduction.

    Three reasons a frame becomes a keyframe, and they are recorded per frame
    so the plan can be audited rather than trusted:
      * `shot_open`   — every shot opens with one, unconditionally. Flow across
                        a cut is meaningless (T11).
      * `flow_failed` — the residual exceeded `residual_max`: occlusion, a fast
                        pan, a limb crossing a body.
      * `chain_limit` — the warp chain hit `max_chain`. Each warp resamples the
                        previous warp's output, so drift accumulates no matter
                        what the residual says.
    """
    if not source_frames:
        raise PropagationError("plan_keyframes got no source frames")
    if len(source_frames) != shot_plan.total_frames:
        raise PropagationError(
            f"{len(source_frames)} source frames but the shot plan covers "
            f"{shot_plan.total_frames}. One of them belongs to a different run."
        )

    boundaries = shot_plan.boundary_frames()
    plans: list[FramePlan] = []
    previous_gray = None
    chain = 0

    for index, path in enumerate(source_frames, start=1):
        shot = shot_plan.shot_for_frame(index)
        current_gray = _gray(path)

        if index == 1 or index in boundaries:
            plans.append(
                FramePlan(index=index, shot_index=shot.index, is_keyframe=True,
                          reason="shot_open", chain_length=0)
            )
            chain = 0
            previous_gray = current_gray
            continue

        residual = flow_residual(previous_gray, current_gray, cfg)
        if residual > residual_max:
            plans.append(
                FramePlan(index=index, shot_index=shot.index, is_keyframe=True,
                          reason="flow_failed", residual=residual, chain_length=0)
            )
            chain = 0
        elif chain + 1 > max_chain:
            plans.append(
                FramePlan(index=index, shot_index=shot.index, is_keyframe=True,
                          reason="chain_limit", residual=residual, chain_length=0)
            )
            chain = 0
        else:
            chain += 1
            plans.append(
                FramePlan(index=index, shot_index=shot.index, is_keyframe=False,
                          reason="propagated", residual=residual, chain_length=chain)
            )
        previous_gray = current_gray

    plan = KeyframePlan(
        total_frames=len(source_frames), frames=plans,
        residual_max=residual_max, max_chain=max_chain,
    )
    if logger is not None:
        logger.info("propagate.planned", **plan.summary())
    return plan


def propagate_frame(
    *,
    source_previous: Path,
    source_current: Path,
    restyled_previous: Path,
    dst: Path,
    cfg: TemporalConfig,
) -> Path:
    """Warp `restyled_previous` into `source_current`'s position. Costs nothing.

    The flow is estimated between the two SOURCE frames — the restyled pair
    would give a different (and worse) estimate, because the restyle changes
    the texture the flow estimator keys on.
    """
    import numpy as np
    from PIL import Image

    previous_gray = _gray(source_previous)
    current_gray = _gray(source_current)
    flow = flow_between(previous_gray, current_gray, cfg)

    with Image.open(restyled_previous) as img:
        restyled = np.asarray(img.convert("RGB"))
    warped = warp(restyled, flow)
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(warped).save(dst)
    return dst


def execute_plan(
    *,
    plan: KeyframePlan,
    source_frames: list[Path],
    out_dir: Path,
    cfg: TemporalConfig,
    restyle_keyframe,
    logger: RunLogger,
) -> dict:
    """Run a keyframe plan. `restyle_keyframe(index, src, dst)` does the paid work.

    Resume-safe the same way `restyle_frames` is: an existing output is never
    regenerated, so a crashed run never re-spends.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paid = 0
    propagated = 0
    resumed = 0

    for frame in plan.frames:
        src = source_frames[frame.index - 1]
        dst = out_dir / src.name
        if dst.is_file():
            resumed += 1
            continue

        if frame.is_keyframe:
            restyle_keyframe(frame.index, src, dst)
            paid += 1
            continue

        previous_src = source_frames[frame.index - 2]
        previous_dst = out_dir / previous_src.name
        if not previous_dst.is_file():
            raise PropagationError(
                f"frame {frame.index} is planned as a warp from frame "
                f"{frame.index - 1}, but {previous_dst.name} does not exist. "
                "A propagation chain must be executed in order."
            )
        propagate_frame(
            source_previous=previous_src, source_current=src,
            restyled_previous=previous_dst, dst=dst, cfg=cfg,
        )
        propagated += 1

    depths = [f.chain_length for f in plan.frames if not f.is_keyframe]
    result = {
        "paid_frames": paid,
        "propagated_frames": propagated,
        "resumed_frames": resumed,
        "total_frames": plan.total_frames,
        # The deepest warp chain is the drift exposure. Reported because TF
        # cannot detect it (see FINDING 1) — a reviewer needs the number.
        "max_chain_depth_used": max(depths) if depths else 0,
        "mean_chain_depth": round(sum(depths) / len(depths), 2) if depths else 0.0,
    }
    logger.info("propagate.executed", **result)
    return result
