"""Versioned YAML config, validated on startup (SPEC Rule 25).

Nothing here falls back to a hardcoded default when a config file is missing or
malformed: a bad config is a startup crash with the offending field named.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

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
    header_height: int = Field(gt=0)
    panel_height: int = Field(gt=0)
    divider_height: int = Field(ge=0)
    crf: int = Field(ge=0, le=51)
    pix_fmt: str
    default_fps: int = Field(gt=0)
    header_font_size: int = Field(gt=0)
    font_candidates: list[Path] = Field(min_length=1)

    @model_validator(mode="after")
    def _geometry_closes(self) -> "RenderConfig":
        total = self.header_height + 2 * self.panel_height + self.divider_height
        if total != self.height:
            raise ValueError(
                f"render geometry does not close: header({self.header_height}) + "
                f"2*panel({self.panel_height}) + divider({self.divider_height}) "
                f"= {total}, expected height {self.height}"
            )
        for name in ("width", "height", "header_height", "panel_height", "divider_height"):
            if getattr(self, name) % 2:
                raise ValueError(f"render.{name} must be even for {self.pix_fmt}")
        return self

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
    # SPEC A4: TBD until the operator supplies it. Never invented.
    drive_folder_id: str | None = None


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


class ScoreThresholds(BaseModel):
    model_config = {"extra": "forbid"}

    pass_: float = Field(alias="pass", ge=0.0, le=1.0)
    borderline: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> "ScoreThresholds":
        if not self.borderline < self.pass_:
            raise ValueError(
                f"thresholds.borderline ({self.borderline}) must be below "
                f"thresholds.pass ({self.pass_})"
            )
        return self


class WeightsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    version: int
    weights: ScoreWeights
    targets: ScoreTargets
    thresholds: ScoreThresholds


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
