"""Stage: fidelity scoring (SPEC §3, brief §6).

The composite Fidelity Score, one number per frame:

    F = w_ssim*SSIM + w_lpips*(1 - LPIPS_edges) + w_id*ID + w_tf*TF

The FORMULA is fixed here in code. Every weight, threshold and tuning parameter
comes from `weights.yaml` — nothing numeric in this module is a policy choice.

Each component defends one named failure mode (brief §9):

    SSIM         structural drift      restyled geometry vs the source frame
    LPIPS_edges  structural drift      perceptual distance on CANNY EDGE MAPS of
                                       both frames, never on raw colour: a
                                       correct restyle SHOULD change colour and
                                       texture, and grading that would punish
                                       exactly the thing we asked for
    ID           identity drift        cosine similarity of a character
                                       embedding against the Stage-0 reference
    TF           temporal flicker      optical-flow-warped difference between
                                       CONSECUTIVE RESTYLED frames

Sync drift (failure mode 5) is absent by design — it is eliminated in
assembly by re-muxing, not measured here.
"""

from __future__ import annotations

import functools
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

import numpy as np

from ..config import CannyConfig, ModelsConfig, TemporalConfig, WeightsConfig


class ScoringError(RuntimeError):
    """Scoring could not be performed. Never silently degraded to a guess."""


class Verdict(str, Enum):
    PASS = "PASS"
    BORDERLINE = "BORDERLINE"
    FAIL = "FAIL"


class VerdictReason(str, Enum):
    """WHY a frame landed where it did.

    The retry policy branches on this, not on the verdict alone: a frame that is
    BORDERLINE because a component target was missed and one that is BORDERLINE
    because the composite dipped are the same verdict but different diagnoses.
    """

    ACCEPTED = "accepted"
    TARGETS_MISSED = "targets_missed"          # Decision 1 route to BORDERLINE
    COMPOSITE_BORDERLINE = "composite_borderline"
    COMPOSITE_FAIL = "composite_fail"


@dataclass(frozen=True)
class FrameScore:
    """One frame's scores, plus why it landed where it did."""

    frame: str
    ssim: float
    lpips_edges: float
    identity: float
    temporal: float
    f: float
    verdict: Verdict
    reason: VerdictReason
    # Per-component target checks (SPEC §3). Under Decision 1 these GATE the
    # verdict; they also say WHICH component missed, which is what makes a
    # non-PASS actionable.
    targets_met: dict[str, bool]

    @property
    def missed_targets(self) -> list[str]:
        return sorted(k for k, ok in self.targets_met.items() if not ok)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        d["reason"] = self.reason.value
        d["missed_targets"] = self.missed_targets
        return d


# --------------------------------------------------------------------------
# Image helpers
# --------------------------------------------------------------------------

def load_image(path: Path) -> np.ndarray:
    """RGB uint8 array. PIL rather than cv2 so channel order is unambiguous."""
    from PIL import Image

    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"))


def _as_gray(img: np.ndarray) -> np.ndarray:
    import cv2

    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)


def _require_same_shape(a: np.ndarray, b: np.ndarray, what: str) -> None:
    if a.shape[:2] != b.shape[:2]:
        raise ScoringError(
            f"{what}: frame sizes differ ({a.shape[:2]} vs {b.shape[:2]}). "
            "A restyle backend must never change dimensions."
        )


def canny_edges(img: np.ndarray, cfg: CannyConfig) -> np.ndarray:
    """Binary edge map. This — not the colour image — is what LPIPS grades."""
    import cv2

    gray = _as_gray(img)
    blurred = cv2.GaussianBlur(gray, (cfg.blur_kernel, cfg.blur_kernel), 0)
    return cv2.Canny(blurred, cfg.low_threshold, cfg.high_threshold)


# --------------------------------------------------------------------------
# Metric 1 — SSIM (structural drift)
# --------------------------------------------------------------------------

def structural_similarity_score(source: np.ndarray, restyled: np.ndarray) -> float:
    from skimage.metrics import structural_similarity as skimage_ssim

    _require_same_shape(source, restyled, "SSIM")
    value = skimage_ssim(_as_gray(source), _as_gray(restyled), data_range=255)
    return float(np.clip(value, 0.0, 1.0))


# --------------------------------------------------------------------------
# Metric 2 — LPIPS on edge maps (structural drift, perceptual)
# --------------------------------------------------------------------------

@runtime_checkable
class PerceptualDistance(Protocol):
    """Perceptual distance between two single-channel maps, in [0, ~1]."""

    name: str

    def distance(self, a: np.ndarray, b: np.ndarray) -> float: ...


class LpipsPerceptualDistance:
    """`lpips` wrapped so it is loaded once and never downloads mid-run.

    Weights are fetched on first construction and cached by torch. A run that
    reaches scoring with a cold cache and no network fails loudly here rather
    than part-way through a batch.
    """

    def __init__(self, net: str = "alex") -> None:
        self.name = f"lpips-{net}"
        self._net = net
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                import lpips
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise ScoringError(
                    "the `lpips` package is required for scoring; install the "
                    "scoring extra: pip install -e '.[scoring]'"
                ) from exc
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self._model = lpips.LPIPS(net=self._net, verbose=False)
            self._model.eval()
        return self._model

    def distance(self, a: np.ndarray, b: np.ndarray) -> float:
        import torch

        model = self._load()
        # lpips expects 3-channel NCHW in [-1, 1]; edge maps are single-channel,
        # so they are replicated across RGB.
        def prep(x: np.ndarray) -> "torch.Tensor":
            arr = x.astype(np.float32) / 255.0
            if arr.ndim == 2:
                arr = np.repeat(arr[None, ...], 3, axis=0)
            else:
                arr = arr.transpose(2, 0, 1)
            return torch.from_numpy(arr * 2.0 - 1.0).unsqueeze(0)

        with torch.no_grad():
            return float(model(prep(a), prep(b)).item())


def lpips_edge_distance(
    source: np.ndarray,
    restyled: np.ndarray,
    model: PerceptualDistance,
    canny: CannyConfig,
) -> float:
    """LPIPS between the CANNY EDGE MAPS of the two frames.

    Never call a perceptual model on the raw colour images here. That is the
    single most load-bearing line in this module: a claymation or LEGO restyle
    is *supposed* to look nothing like the source in colour and texture.
    """
    _require_same_shape(source, restyled, "LPIPS_edges")
    return float(
        np.clip(model.distance(canny_edges(source, canny), canny_edges(restyled, canny)), 0.0, 1.0)
    )


# --------------------------------------------------------------------------
# Metric 3 — identity (character drift)
# --------------------------------------------------------------------------

@runtime_checkable
class IdentityEmbedder(Protocol):
    name: str

    def embed(self, image: np.ndarray) -> np.ndarray: ...


class OpenClipEmbedder:
    """open_clip image embeddings.

    open_clip rather than insightface: insightface is a HUMAN FACE recogniser
    and will frequently detect no face at all on a claymation or LEGO
    character, which is precisely the material this pipeline produces. A
    general image embedding degrades gracefully where a face detector returns
    nothing at all.
    """

    def __init__(self, arch: str = "ViT-B-32", pretrained: str = "openai") -> None:
        self.name = f"open_clip-{arch}-{pretrained}"
        self._arch = arch
        self._pretrained = pretrained
        self._model = None
        self._preprocess = None

    def _load(self):
        if self._model is None:
            try:
                import open_clip
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise ScoringError(
                    "the `open_clip_torch` package is required for identity "
                    "scoring; install the scoring extra: pip install -e '.[scoring]'"
                ) from exc
            model, _, preprocess = open_clip.create_model_and_transforms(
                self._arch, pretrained=self._pretrained
            )
            model.eval()
            self._model, self._preprocess = model, preprocess
        return self._model, self._preprocess

    def embed(self, image: np.ndarray) -> np.ndarray:
        import torch
        from PIL import Image

        model, preprocess = self._load()
        tensor = preprocess(Image.fromarray(image)).unsqueeze(0)
        with torch.no_grad():
            vec = model.encode_image(tensor)
        vec = vec / vec.norm(dim=-1, keepdim=True)
        return vec.squeeze(0).numpy().astype(np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def identity_similarity(
    restyled: np.ndarray,
    references: Sequence[np.ndarray],
    embedder: IdentityEmbedder,
) -> float:
    """Cosine similarity against the Stage-0 locked references.

    Several references are the normal case (a character in different poses), so
    the BEST match wins: the character need only be recognisable as one of the
    locked views, not as all of them simultaneously.
    """
    if not references:
        raise ScoringError(
            "identity scoring needs at least one Stage-0 reference image. "
            "F weights ID at 0.20 and the formula is fixed, so there is no "
            "defined score without one. Lock references at intake, or raise "
            "the question of how styles with no character should be scored."
        )
    target = embedder.embed(restyled)
    best = max(cosine_similarity(target, embedder.embed(ref)) for ref in references)
    return float(np.clip(best, 0.0, 1.0))


# --------------------------------------------------------------------------
# Metric 4 — temporal fidelity (flicker)
# --------------------------------------------------------------------------

def motion_compensated_residual(
    previous_restyled: np.ndarray,
    current_restyled: np.ndarray,
    cfg: TemporalConfig,
) -> np.ndarray:
    """Per-pixel |warped(prev) - curr| after cancelling real motion with flow."""
    import cv2

    _require_same_shape(previous_restyled, current_restyled, "TF")
    prev_gray = _as_gray(previous_restyled)
    curr_gray = _as_gray(current_restyled)

    flow = cv2.calcOpticalFlowFarneback(
        prev_gray, curr_gray, None,
        cfg.pyr_scale, cfg.levels, cfg.winsize, cfg.iterations,
        cfg.poly_n, cfg.poly_sigma, 0,
    )
    h, w = prev_gray.shape
    grid_x, grid_y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    map_x = (grid_x + flow[..., 0]).astype(np.float32)
    map_y = (grid_y + flow[..., 1]).astype(np.float32)
    warped = cv2.remap(prev_gray, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return np.abs(warped.astype(np.float32) - curr_gray.astype(np.float32))


def _block_p95(residual: np.ndarray, block: int) -> float:
    """Median of per-block 95th percentiles.

    This is what actually separates flicker from motion (D34). Shimmer raises
    the high percentile in MOST blocks, so the median rises with it. Real motion
    raises it enormously in the FEW blocks containing occlusion edges, and the
    median steps straight over them. A whole-frame p95 cannot make that
    distinction — it reports the occlusion edges and nothing else.
    """
    h, w = residual.shape
    h -= h % block
    w -= w % block
    if h == 0 or w == 0:  # frame smaller than one block
        return float(np.percentile(residual, 95))
    tiles = (
        residual[:h, :w]
        .reshape(h // block, block, w // block, block)
        .transpose(0, 2, 1, 3)
        .reshape(-1, block * block)
    )
    return float(np.median(np.percentile(tiles, 95, axis=1)))


def temporal_fidelity(
    previous_restyled: np.ndarray,
    current_restyled: np.ndarray,
    cfg: TemporalConfig,
) -> float:
    """1 - normalised motion-compensated difference between consecutive frames.

    Real motion is cancelled by warping the previous frame along the estimated
    flow before differencing, so what remains is flicker: the restyle changing
    its mind about a surface that did not actually change.

    HOW the residual is reduced to one number is configurable and matters more
    than it looks — see `temporal.aggregation` in weights.yaml and memory.md D34.
    """
    residual = motion_compensated_residual(previous_restyled, current_restyled, cfg)

    if cfg.aggregation == "block_p95":
        statistic = _block_p95(residual, cfg.block_size)
    elif cfg.aggregation == "p95":
        statistic = float(np.percentile(residual, 95))
    else:
        statistic = float(residual.mean())

    return float(np.clip(1.0 - statistic / 255.0, 0.0, 1.0))


# The first frame of a shot has no predecessor to compare against. It is scored
# as temporally perfect rather than penalised for existing; the frame that
# follows it carries the real temporal signal.
FIRST_FRAME_TEMPORAL_FIDELITY = 1.0


# --------------------------------------------------------------------------
# Composite
# --------------------------------------------------------------------------

def composite_f(
    *, ssim: float, lpips_edges: float, identity: float, temporal: float, cfg: WeightsConfig
) -> float:
    """The fixed formula. Weights come from config; the shape never does."""
    w = cfg.weights
    value = (
        w.ssim * ssim
        + w.lpips_edges * (1.0 - lpips_edges)
        + w.id * identity
        + w.tf * temporal
    )
    return float(np.clip(value, 0.0, 1.0))


def classify(
    f: float, cfg: WeightsConfig, targets_met: dict[str, bool] | None = None
) -> tuple[Verdict, VerdictReason]:
    """The two-step gate. Order is defined in weights.yaml and mirrored here.

    0. FAIL is a composite-only veto: F < `borderline` -> FAIL, decided first,
       because a component miss must never be the thing that FAILs a frame.
    1. Component targets next. Any miss -> BORDERLINE (targets_missed). When a
       frame both misses a target and sits below the pass line, the missed
       target is reported, being the more actionable diagnosis.
    2. Composite F last. F < `pass` -> BORDERLINE (composite_borderline).

    `targets_met` is optional so the composite bands stay testable on their own;
    when it is omitted, only step 2 applies.
    """
    t = cfg.thresholds
    # FAIL is a composite-only veto and takes precedence: a component miss can
    # never be the thing that FAILs a frame, so the sub-borderline case is
    # settled before the target check runs.
    if f < t.borderline:
        return Verdict.FAIL, VerdictReason.COMPOSITE_FAIL
    # Targets next, ahead of the F band. A missed target is the more actionable
    # diagnosis, so it wins the reason when both are true.
    if t.require_component_targets and targets_met is not None and not all(targets_met.values()):
        return Verdict.BORDERLINE, VerdictReason.TARGETS_MISSED
    if f < t.pass_:
        return Verdict.BORDERLINE, VerdictReason.COMPOSITE_BORDERLINE
    return Verdict.PASS, VerdictReason.ACCEPTED


def check_targets(
    *, ssim: float, lpips_edges: float, identity: float, temporal: float, cfg: WeightsConfig
) -> dict[str, bool]:
    t = cfg.targets
    return {
        "ssim": ssim >= t.ssim_min,
        "lpips_edges": lpips_edges <= t.lpips_edges_max,
        "id": identity >= t.id_min,
        "tf": temporal >= t.tf_min,
    }


@dataclass
class Scorer:
    """Holds the two learned models so they load once per run, not per frame."""

    cfg: WeightsConfig
    perceptual: PerceptualDistance
    embedder: IdentityEmbedder

    @classmethod
    def build(cls, cfg: WeightsConfig) -> "Scorer":
        m: ModelsConfig = cfg.models
        return cls(
            cfg=cfg,
            perceptual=LpipsPerceptualDistance(net=m.lpips_net),
            embedder=OpenClipEmbedder(arch=m.identity_model, pretrained=m.identity_pretrained),
        )

    def score_frame(
        self,
        *,
        frame: str,
        source: np.ndarray,
        restyled: np.ndarray,
        references: Sequence[np.ndarray],
        previous_restyled: np.ndarray | None,
    ) -> FrameScore:
        ssim = structural_similarity_score(source, restyled)
        lpips_edges = lpips_edge_distance(source, restyled, self.perceptual, self.cfg.canny)
        identity = identity_similarity(restyled, references, self.embedder)
        temporal = (
            FIRST_FRAME_TEMPORAL_FIDELITY
            if previous_restyled is None
            else temporal_fidelity(previous_restyled, restyled, self.cfg.temporal)
        )
        f = composite_f(
            ssim=ssim, lpips_edges=lpips_edges, identity=identity,
            temporal=temporal, cfg=self.cfg,
        )
        targets_met = check_targets(
            ssim=ssim, lpips_edges=lpips_edges, identity=identity,
            temporal=temporal, cfg=self.cfg,
        )
        verdict, reason = classify(f, self.cfg, targets_met)
        return FrameScore(
            frame=frame,
            ssim=ssim,
            lpips_edges=lpips_edges,
            identity=identity,
            temporal=temporal,
            f=f,
            verdict=verdict,
            reason=reason,
            targets_met=targets_met,
        )


@functools.lru_cache(maxsize=1)
def models_are_cached() -> bool:
    """Whether both learned models can be built right now.

    A pure predicate — it changes no global state. To guarantee the check (and
    the scoring that follows) cannot reach the network, set `HF_HUB_OFFLINE=1`
    in the environment first; the test suite does exactly that.

    Scoring must never download mid-batch: warm the cache deliberately, before
    a run starts spending.
    """
    try:
        from ..config import load_weights

        cfg = load_weights()
        LpipsPerceptualDistance(net=cfg.models.lpips_net)._load()
        OpenClipEmbedder(
            arch=cfg.models.identity_model, pretrained=cfg.models.identity_pretrained
        )._load()
        return True
    except Exception:
        return False
