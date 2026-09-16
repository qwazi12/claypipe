"""Versioned YAML config, validated on startup (SPEC Rule 25).

Nothing here falls back to a hardcoded default when a config file is missing or
malformed: a bad config is a startup crash with the offending field named.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
STYLES_PATH = REPO_ROOT / "styles.yaml"
WEIGHTS_PATH = REPO_ROOT / "weights.yaml"

HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]


class ConfigError(RuntimeError):
    """Raised when a config file is missing, unparseable, or invalid."""


class RenderConfig(BaseModel):
    model_config = {"extra": "forbid"}

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    # T9: panel and margin heights are DERIVED from the source clip's aspect
    # ratio, not configured. See claypipe/pipeline/layout.py and MASTER_PLAN
    # §1.1. The only vertical figure still under operator control is how much
    # of the canvas the caption band gets.
    caption_gap_fraction: float = Field(ge=0.0, lt=0.5)
    crf: int = Field(ge=0, le=51)
    pix_fmt: str
    default_fps: int = Field(gt=0)
    header_font_size: int = Field(gt=0)
    caption_font_size: int = Field(gt=0)
    font_candidates: list[Path] = Field(min_length=1)

    @model_validator(mode="after")
    def _canvas_is_even(self) -> "RenderConfig":
        for name in ("width", "height"):
            if getattr(self, name) % 2:
                raise ValueError(f"render.{name} must be even for {self.pix_fmt}")
        return self

    def layout_for(self, source_aspect: float):
        """Resolve the vertical stack for a source of this aspect ratio."""
        from .pipeline.layout import compute_layout

        return compute_layout(
            canvas_width=self.width,
            canvas_height=self.height,
            source_aspect=source_aspect,
            gap_fraction=self.caption_gap_fraction,
        )

    def font_path(self) -> Path:
        """First existing font candidate. No silent fallback to a default font."""
        for candidate in self.font_candidates:
            if candidate.is_file():
                return candidate
        raise ConfigError(
            "No usable font found. Tried: "
            + ", ".join(str(c) for c in self.font_candidates)
            + " — add a valid .ttf path to render.font_candidates in styles.yaml"
        )


class OutputConfig(BaseModel):
    model_config = {"extra": "forbid"}

    runs_dir: Path


class StyleProfile(BaseModel):
    model_config = {"extra": "forbid"}

    prompt: str = Field(min_length=1)
    strength: float = Field(gt=0.0, le=1.0)
    background_color: HexColor
    header_text_color: HexColor


class StylesConfig(BaseModel):
    model_config = {"extra": "forbid"}

    version: int
    render: RenderConfig
    output: OutputConfig
    styles: dict[str, StyleProfile] = Field(min_length=1)

    def profile(self, name: str) -> StyleProfile:
        try:
            return self.styles[name]
        except KeyError:
            raise ConfigError(
                f"unknown style {name!r}; styles.yaml defines: {sorted(self.styles)}"
            ) from None


class ScoreWeights(BaseModel):
    model_config = {"extra": "forbid"}

    ssim: float = Field(ge=0.0, le=1.0)
    lpips_edges: float = Field(ge=0.0, le=1.0)
    id: float = Field(ge=0.0, le=1.0)
    tf: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _sums_to_one(self) -> "ScoreWeights":
        total = self.ssim + self.lpips_edges + self.id + self.tf
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class ScoreTargets(BaseModel):
    model_config = {"extra": "forbid"}

    ssim_min: float
    lpips_edges_max: float
    id_min: float
    tf_min: float
    # V4: motion sync against the source's flow field. The primary gate under
    # whole-frame v2v. Defaults to 0.0 so a config written before V4 loads and
    # reports FLOW without gating on an invented number — the value has to come
    # from a canary measurement, not from here.
    flow_min: float = 0.0


# T13/A1 — the scoring gate is MODE-DEPENDENT.
#
# SSIM 0.72 is right for Track A (surface restyle: geometry preserved, so
# structure preservation is the goal) and WRONG for Track C (video-native
# resynthesis: a resynthesised frame legitimately moves geometry, so the
# surface targets would reject good output). One target vector cannot serve
# both, and shipping guessed thresholds on a paid backend is exactly the
# failure mode finding F4 warns about.
Mode = Literal["surface", "resynth"]


class ModeConfig(BaseModel):
    """One track's target vector and retry policy."""

    model_config = {"extra": "forbid"}

    targets: ScoreTargets
    aggregation: Literal["block_p95", "p95", "mean"] = "block_p95"

    # A1/A4: the retry policy differs FUNDAMENTALLY between modes. A failed
    # frame reseeds one frame; a failed clip chunk reseeds 81-240 frames at
    # once, so a single Track C retry can consume more budget than ten Track A
    # retries. Sharing one cap would let one chunk failure eat the run.
    max_retries_per_unit: int = Field(ge=0)
    total_retry_budget_fraction: float = Field(gt=0.0, le=1.0)

    # F4 FIREWALL. False means these numbers have the right SHAPE but were
    # never measured — they are placeholders. A paid run in an uncalibrated
    # mode is refused: see `WeightsConfig.assert_mode_is_spendable`. T16 is the
    # first real measurement; set the numbers from it and flip this to true in
    # the same commit that records the decision.
    calibrated: bool = False
    calibration_note: str = ""

    @model_validator(mode="after")
    def _uncalibrated_modes_must_explain_themselves(self) -> "ModeConfig":
        if not self.calibrated and not self.calibration_note.strip():
            raise ValueError(
                "an uncalibrated mode must carry a calibration_note saying "
                "where its numbers came from and what will replace them — "
                "otherwise a future session cannot tell a placeholder from a "
                "measurement"
            )
        return self


class ScoreThresholds(BaseModel):
    model_config = {"extra": "forbid"}

    pass_: float = Field(alias="pass", ge=0.0, le=1.0)
    borderline: float = Field(ge=0.0, le=1.0)
    # Decision 1 (2026-09-09): PASS requires F >= pass AND every component
    # target met. See the gate-order comment in weights.yaml.
    require_component_targets: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> "ScoreThresholds":
        if not self.borderline < self.pass_:
            raise ValueError(
                f"thresholds.borderline ({self.borderline}) must be below "
                f"thresholds.pass ({self.pass_})"
            )
        return self


class CannyConfig(BaseModel):
    model_config = {"extra": "forbid"}

    blur_kernel: int = Field(gt=0)
    low_threshold: int = Field(ge=0, le=255)
    high_threshold: int = Field(ge=0, le=255)

    @model_validator(mode="after")
    def _checks(self) -> "CannyConfig":
        if self.blur_kernel % 2 == 0:
            raise ValueError(f"canny.blur_kernel must be odd, got {self.blur_kernel}")
        if self.low_threshold >= self.high_threshold:
            raise ValueError(
                f"canny.low_threshold ({self.low_threshold}) must be below "
                f"high_threshold ({self.high_threshold})"
            )
        return self


class TemporalConfig(BaseModel):
    model_config = {"extra": "forbid"}

    aggregation: Literal["block_p95", "p95", "mean"] = "block_p95"
    block_size: int = Field(default=4, gt=1)
    pyr_scale: float = Field(gt=0.0, lt=1.0)
    levels: int = Field(gt=0)
    winsize: int = Field(gt=0)
    iterations: int = Field(gt=0)
    poly_n: int = Field(gt=0)
    poly_sigma: float = Field(gt=0.0)


class ModelsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    lpips_net: str
    identity_model: str
    identity_pretrained: str


class KillSwitchConfig(BaseModel):
    model_config = {"extra": "forbid"}

    after_frames: int = Field(gt=0)
    min_mean_f: float = Field(ge=0.0, le=1.0)


PriceUnit = Literal["image", "megapixel", "video_second", "frames_div_16"]

# The divisor behind the `frames_div_16` unit.
#
# VERIFIED VERBATIM on fal's Wan VACE 14B model page and its llms.txt, both on
# 2026-09-16: "Video seconds are calculated at 16 frames per second."
#
# This is NOT wall-clock duration, and the difference is a 2x cost swing on
# every run. A 60-second clip at our 12fps cadence is 720 frames, which bills
# as 720/16 = 45 video-seconds ($1.80 at 480p) — not as 60 wall-clock seconds
# ($2.40). Guessing this wrong in either direction breaks the spend caps: too
# low and they stop binding, too high and the affordability ceiling refuses
# every real call.
BILLED_FRAMES_PER_SECOND = 16


class PriceModel(BaseModel):
    """What a backend charges, and WHAT IT CHARGES FOR (T15/F3).

    A flat per-call number silently mis-prices two thirds of the candidate
    backends. fal Kontext [dev] bills per megapixel; Wan VACE bills per
    FRAME COUNT divided by 16. Pricing either as "per call" produces a ledger
    that reconciles to the wrong figure and spend caps that do not bind.

    THE TWO VIDEO UNITS ARE NOT INTERCHANGEABLE, and conflating them is a 2x
    error:
      `video_second`  — wall-clock duration of the output. What Qwen Cloud's
                        Wan 3.0 bills ($0.035/sec at 480p).
      `frames_div_16` — frame count / 16, regardless of the fps the output is
                        played at. What fal's Wan VACE and Wan Animate bill.
    At our 12fps cadence a 60s clip is 720 frames: 45 billed seconds under
    frames_div_16, 60 under video_second. Both units exist here so neither
    backend has to be approximated by the other.

    `round_up_to_mp` is not a rounding preference — it is fal's actual billing
    rule, and it inverts an optimisation: fal rounds UP to the next whole
    megapixel, so 640x640 (0.41MP) pays the 1MP rate. On a hosted per-MP
    backend the cheap move is therefore to render LARGE (you are paying for
    1MP either way); on self-hosted GPU, cost is roughly linear in pixels, so
    the cheap move is to render SMALL. The same render geometry has opposite
    optima depending on the backend, which is why geometry cannot be a fixed
    aesthetic constant.
    """

    model_config = {"extra": "forbid"}

    unit: PriceUnit
    rate: float = Field(ge=0.0)
    round_up_to_mp: bool = False

    @model_validator(mode="after")
    def _round_up_only_applies_to_megapixels(self) -> "PriceModel":
        if self.round_up_to_mp and self.unit != "megapixel":
            raise ValueError(
                f"round_up_to_mp is meaningless for unit {self.unit!r} — it "
                "describes a per-megapixel billing rule"
            )
        return self

    def cost(
        self,
        *,
        megapixels: float | None = None,
        video_seconds: float | None = None,
        frames: int | None = None,
    ) -> float:
        """Price one call. The required dimension is NOT optional: asking for a
        per-video-second price without a duration is a bug, and defaulting it
        to 1 would under-report spend by whatever the real duration was."""
        if self.unit == "image":
            return self.rate
        if self.unit == "megapixel":
            if megapixels is None:
                raise ConfigError(
                    "this backend bills per megapixel; the render geometry must "
                    "be supplied. Refusing to price a call against an assumed size."
                )
            billable = math.ceil(megapixels) if self.round_up_to_mp else megapixels
            return self.rate * billable
        if self.unit == "frames_div_16":
            if frames is None:
                raise ConfigError(
                    "this backend bills per frame-count/"
                    f"{BILLED_FRAMES_PER_SECOND}; the FRAME COUNT must be "
                    "supplied, not a duration. Passing a wall-clock duration "
                    "here would misprice the call by the ratio between the "
                    "output fps and "
                    f"{BILLED_FRAMES_PER_SECOND} — a 2x error at 12fps."
                )
            return self.rate * frames / BILLED_FRAMES_PER_SECOND
        if video_seconds is None:
            raise ConfigError(
                "this backend bills per video-second; the clip duration must be "
                "supplied. Refusing to price a call against an assumed duration."
            )
        return self.rate * video_seconds

    def billed_units(
        self,
        *,
        megapixels: float | None = None,
        video_seconds: float | None = None,
        frames: int | None = None,
    ) -> float:
        """How many billable units this call consumes, for the ledger record.

        Recorded alongside the dollar figure so a bill can be reconciled: an
        entry saying only "$1.80" cannot distinguish 45 billed seconds at
        $0.04 from 22.5 at $0.08."""
        if self.unit == "image":
            return 1.0
        if self.unit == "megapixel":
            if megapixels is None:
                return 0.0
            return float(math.ceil(megapixels) if self.round_up_to_mp else megapixels)
        if self.unit == "frames_div_16":
            return 0.0 if frames is None else frames / BILLED_FRAMES_PER_SECOND
        return 0.0 if video_seconds is None else float(video_seconds)


class CostConfig(BaseModel):
    model_config = {"extra": "forbid"}

    estimated_usd_per_call: dict[str, float]
    # T15: unit-aware pricing. `estimated_usd_per_call` remains the per-IMAGE
    # shorthand every existing caller uses; `pricing` is what a non-per-image
    # backend needs. The validator below keeps them from disagreeing, so there
    # is one canonical number per backend even though there are two spellings.
    pricing: dict[str, PriceModel] = Field(default_factory=dict)
    max_cost_usd_run: float | None = None
    max_cost_usd_project: float | None = None

    @model_validator(mode="after")
    def _non_negative(self) -> "CostConfig":
        for backend, usd in self.estimated_usd_per_call.items():
            if usd < 0:
                raise ValueError(f"cost estimate for {backend!r} is negative: {usd}")
        for name in ("max_cost_usd_run", "max_cost_usd_project"):
            cap = getattr(self, name)
            if cap is not None and cap <= 0:
                raise ValueError(f"firewalls.cost.{name} must be positive or null, got {cap}")
        # One canonical number per backend: a per-image entry in both places
        # must agree, or the ledger and the backend would quote different
        # prices for the same call.
        for backend, model in self.pricing.items():
            if model.unit != "image" or backend not in self.estimated_usd_per_call:
                continue
            flat = self.estimated_usd_per_call[backend]
            if abs(flat - model.rate) > 1e-9:
                raise ValueError(
                    f"backend {backend!r} is priced twice and the two disagree: "
                    f"estimated_usd_per_call={flat} but pricing.rate={model.rate}. "
                    "One canonical number per backend."
                )
        return self

    def per_call(self, backend: str) -> float:
        """Estimated spend for one PER-IMAGE call. An unpriced backend is a hard
        error: an unknown price must never be silently treated as free.

        A backend that does NOT bill per image is refused here rather than
        answered, because answering would hand back a per-image figure for a
        per-megapixel or per-video-second charge — a ledger that reconciles to
        the wrong number and spend caps that do not bind (F3)."""
        model = self.pricing.get(backend)
        if model is not None and model.unit != "image":
            raise ConfigError(
                f"backend {backend!r} bills per {model.unit}, not per call. Use "
                "price_for() with the render geometry or clip duration. Refusing "
                "to return a per-image price for a per-"
                f"{model.unit} charge — that is how a ledger silently reconciles "
                "to the wrong figure."
            )
        if model is not None:
            return model.cost()
        try:
            return self.estimated_usd_per_call[backend]
        except KeyError:
            raise ConfigError(
                f"no cost estimate for backend {backend!r} in "
                f"firewalls.cost.estimated_usd_per_call or firewalls.cost.pricing "
                f"(have: {sorted(set(self.estimated_usd_per_call) | set(self.pricing))}). "
                "Refusing to spend against an unknown price."
            ) from None

    def price_for(
        self,
        backend: str,
        *,
        megapixels: float | None = None,
        video_seconds: float | None = None,
        frames: int | None = None,
    ) -> float:
        """Unit-aware price for one call. The path a non-per-image backend takes."""
        model = self.pricing.get(backend)
        if model is None:
            # No unit declared: fall back to the per-image shorthand, which
            # still refuses an entirely unpriced backend.
            return self.per_call(backend)
        return model.cost(
            megapixels=megapixels, video_seconds=video_seconds, frames=frames
        )

    def billed_units_for(
        self,
        backend: str,
        *,
        megapixels: float | None = None,
        video_seconds: float | None = None,
        frames: int | None = None,
    ) -> float:
        model = self.pricing.get(backend)
        if model is None:
            return 1.0
        return model.billed_units(
            megapixels=megapixels, video_seconds=video_seconds, frames=frames
        )

    def unit_for(self, backend: str) -> PriceUnit:
        model = self.pricing.get(backend)
        return model.unit if model is not None else "image"


class FalModelConfig(BaseModel):
    model_config = {"extra": "forbid"}

    model: str = Field(min_length=1)


class FirewallConfig(BaseModel):
    model_config = {"extra": "forbid"}

    fal: FalModelConfig | None = None
    max_retries_per_frame: int = Field(ge=0)
    total_retry_budget_fraction: float = Field(gt=0.0, le=1.0)
    borderline_strength_delta: float = Field(lt=0.0)
    kill_switch: KillSwitchConfig
    cost: CostConfig


class ReviewConfig(BaseModel):
    model_config = {"extra": "forbid"}

    outlier_f_threshold: float = Field(ge=0.0, le=1.0)
    audit_sample_fraction: float = Field(ge=0.0, le=1.0)
    audit_sample_seed: int
    max_outlier_cards: int = Field(gt=0)
    canary_timeout_s: int = Field(gt=0)
    canary_poll_s: float = Field(gt=0.0)


class WeightsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    version: int
    weights: ScoreWeights
    targets: ScoreTargets
    thresholds: ScoreThresholds
    canny: CannyConfig
    temporal: TemporalConfig
    models: ModelsConfig
    firewalls: FirewallConfig
    review: ReviewConfig
    # T13: per-mode target vectors. `surface` MUST restate `targets` exactly —
    # the validator below enforces it — so there is one canonical number per
    # component even though Track A's numbers now appear in two places.
    modes: dict[str, ModeConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _surface_mode_matches_the_canonical_targets(self) -> "WeightsConfig":
        surface = self.modes.get("surface")
        if surface is None:
            return self
        for field_name in ("ssim_min", "lpips_edges_max", "id_min", "tf_min"):
            canonical = getattr(self.targets, field_name)
            mode_value = getattr(surface.targets, field_name)
            if abs(canonical - mode_value) > 1e-9:
                raise ValueError(
                    f"modes.surface.targets.{field_name} ({mode_value}) does not "
                    f"match targets.{field_name} ({canonical}). Track A's target "
                    "vector has one canonical home; the mode block restates it "
                    "so Track C can differ, not so the two can drift."
                )
        if surface.aggregation != self.temporal.aggregation:
            raise ValueError(
                f"modes.surface.aggregation ({surface.aggregation}) does not match "
                f"temporal.aggregation ({self.temporal.aggregation})"
            )
        if surface.max_retries_per_unit != self.firewalls.max_retries_per_frame:
            raise ValueError(
                f"modes.surface.max_retries_per_unit ({surface.max_retries_per_unit}) "
                f"does not match firewalls.max_retries_per_frame "
                f"({self.firewalls.max_retries_per_frame})"
            )
        if abs(
            surface.total_retry_budget_fraction
            - self.firewalls.total_retry_budget_fraction
        ) > 1e-9:
            raise ValueError(
                "modes.surface.total_retry_budget_fraction does not match "
                "firewalls.total_retry_budget_fraction"
            )
        return self

    def mode(self, name: str) -> ModeConfig:
        try:
            return self.modes[name]
        except KeyError:
            raise ConfigError(
                f"unknown mode {name!r}; weights.yaml defines: {sorted(self.modes)}"
            ) from None

    def assert_mode_is_spendable(self, name: str) -> ModeConfig:
        """F4 FIREWALL: refuse a PAID run in a mode whose targets were never
        measured.

        An uncalibrated mode's numbers have the right shape and no evidence
        behind them. Batching against them would gate real money on a guess,
        and the gate would either pass everything or reject everything — both
        of which look like a working run until the bill arrives."""
        cfg = self.mode(name)
        if not cfg.calibrated:
            raise ConfigError(
                f"mode {name!r} is NOT CALIBRATED. Its target vector is a "
                f"placeholder with the right shape and no measurement behind "
                f"it:\n    {cfg.calibration_note.strip()}\n"
                "Refusing to gate real money on a guess (finding F4). Run the "
                "T16 canary bake-off, set the numbers from what it measures, "
                "and flip modes."
                f"{name}.calibrated to true in the same commit that records the "
                "decision."
            )
        return cfg


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_styles(path: Path = STYLES_PATH) -> StylesConfig:
    try:
        return StylesConfig.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"{path} failed validation:\n{exc}") from exc


def load_weights(path: Path = WEIGHTS_PATH) -> WeightsConfig:
    try:
        return WeightsConfig.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"{path} failed validation:\n{exc}") from exc


def load_all() -> tuple[StylesConfig, WeightsConfig]:
    """Validate every config file at startup, before any work begins."""
    return load_styles(), load_weights()


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
