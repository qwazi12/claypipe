"""Config validation tests (Rule 25: validated on startup, never a silent default)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from claypipe.config import (
    ConfigError,
    StylesConfig,
    WeightsConfig,
    load_styles,
    load_weights,
)


def test_shipped_config_is_valid() -> None:
    styles, weights = load_styles(), load_weights()
    assert set(styles.styles) >= {"clay", "lego", "logo"}
    assert (styles.render.width, styles.render.height) == (1080, 1920)
    assert styles.render.font_path().is_file()


def test_prompts_and_weights_live_in_yaml_not_code() -> None:
    """SPEC Hard Rules: all thresholds/weights/prompts in YAML."""
    raw = yaml.safe_load(Path("styles.yaml").read_text())
    assert "claymation" in raw["styles"]["clay"]["prompt"]
    src = Path("claypipe").rglob("*.py")
    for path in src:
        text = path.read_text()
        assert "claymation, plasticine" not in text, f"{path} hardcodes a style prompt"


def test_weights_must_sum_to_one() -> None:
    raw = yaml.safe_load(Path("weights.yaml").read_text())
    assert sum(raw["weights"].values()) == pytest.approx(1.0)
    raw["weights"]["ssim"] = 0.90
    with pytest.raises(Exception, match="sum to 1.0"):
        WeightsConfig.model_validate(raw)


def test_thresholds_must_be_ordered() -> None:
    raw = yaml.safe_load(Path("weights.yaml").read_text())
    raw["thresholds"]["borderline"] = 0.95
    with pytest.raises(Exception, match="must be below"):
        WeightsConfig.model_validate(raw)


def test_render_geometry_must_close() -> None:
    """header + 2*panel + divider must equal the canvas height, or startup fails."""
    raw = yaml.safe_load(Path("styles.yaml").read_text())
    raw["render"]["panel_height"] = 800
    with pytest.raises(Exception, match="geometry does not close"):
        StylesConfig.model_validate(raw)


def test_unknown_style_is_a_hard_error() -> None:
    with pytest.raises(ConfigError, match="unknown style"):
        load_styles().profile("nope")


def test_missing_config_file_is_a_hard_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing config file"):
        load_styles(tmp_path / "nope.yaml")

