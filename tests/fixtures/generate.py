"""Deterministic synthetic fixtures for the scoring tests.

Every image is drawn from fixed arithmetic — no RNG, no photography, no third
party assets — so the hand-written expected bands in `test_score.py` stay
pinned to exactly these pixels. Regenerate with:

    .venv/bin/python tests/fixtures/generate.py

Each case is a directory holding `source.png` + `restyled.png` (a restyle
result to score against its source), or `prev.png` + `curr.png` (two
consecutive restyled frames, for temporal fidelity).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

SIZE = 256
FIXTURES = Path(__file__).parent


def _canvas() -> np.ndarray:
    """A 'character on a set': gradient backdrop, body, head, eyes, props.

    Deliberately geometric — the metrics under test care about structure, and a
    synthetic scene makes 'geometry moved' and 'colour changed' separable in a
    way a photograph never would be.
    """
    img = np.zeros((SIZE, SIZE, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]

    # Backdrop: vertical gradient.
    img[..., 0] = (40 + yy * 0.35).astype(np.uint8)
    img[..., 1] = (60 + yy * 0.25).astype(np.uint8)
    img[..., 2] = (110 - yy * 0.15).astype(np.uint8)

    # Floor band.
    img[190:, :] = (92, 74, 58)

    # Body: a rectangle.
    img[120:200, 96:160] = (200, 60, 60)

    # Head: a disc.
    head = (xx - 128) ** 2 + (yy - 96) ** 2 < 34**2
    img[head] = (235, 200, 160)

    # Eyes.
    for cx in (116, 140):
        eye = (xx - cx) ** 2 + (yy - 90) ** 2 < 6**2
        img[eye] = (20, 20, 30)

    # Mouth.
    img[110:114, 118:138] = (120, 40, 40)

    # Prop: a box stage-left.
    img[150:190, 20:64] = (70, 150, 90)

    return img


def _translate(img: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Shift the whole frame, edge-padding what rolls in."""
    out = np.roll(img, shift=(dy, dx), axis=(0, 1))
    if dy > 0:
        out[:dy] = img[0]
    elif dy < 0:
        out[dy:] = img[-1]
    if dx > 0:
        out[:, :dx] = out[:, dx : dx + 1]
    elif dx < 0:
        out[:, dx:] = out[:, dx - 1 : dx]
    return out


def _hue_rotate(img: np.ndarray, degrees: float) -> np.ndarray:
    """Rotate hue while holding luminance constant.

    This is the shape of a CORRECT restyle: the palette changes completely, the
    geometry does not. LPIPS-on-edges must not punish it.
    """
    arr = img.astype(np.float32) / 255.0
    lum = arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    cos_a, sin_a = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
    # YIQ hue rotation matrix.
    m = np.array(
        [
            [0.299 + 0.701 * cos_a + 0.168 * sin_a,
             0.587 - 0.587 * cos_a + 0.330 * sin_a,
             0.114 - 0.114 * cos_a - 0.497 * sin_a],
            [0.299 - 0.299 * cos_a - 0.328 * sin_a,
             0.587 + 0.413 * cos_a + 0.035 * sin_a,
             0.114 - 0.114 * cos_a + 0.292 * sin_a],
            [0.299 - 0.300 * cos_a + 1.250 * sin_a,
             0.587 - 0.588 * cos_a - 1.050 * sin_a,
             0.114 + 0.886 * cos_a - 0.203 * sin_a],
        ],
        dtype=np.float32,
    )
    out = arr @ m.T
    # Re-impose the original luminance so grayscale structure is untouched.
    new_lum = out @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    out = out + (lum - new_lum)[..., None]
    return np.clip(out * 255.0, 0, 255).astype(np.uint8)


def _gaussian_blur(img: np.ndarray, radius: int) -> np.ndarray:
    return np.asarray(Image.fromarray(img).filter(__import__("PIL.ImageFilter", fromlist=["x"]).GaussianBlur(radius)))


def _gamma(img: np.ndarray, gamma: float) -> np.ndarray:
    lut = np.clip(((np.arange(256) / 255.0) ** gamma) * 255.0, 0, 255).astype(np.uint8)
    return lut[img]


def _swap_face(img: np.ndarray) -> np.ndarray:
    """Same composition, different character: new head colour, shape and eyes."""
    out = img.copy()
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    head = (xx - 128) ** 2 + (yy - 96) ** 2 < 34**2
    out[head] = (90, 190, 210)                      # different skin
    tall = ((xx - 128) ** 2) / 20**2 + ((yy - 96) ** 2) / 40**2 < 1
    out[tall] = (90, 190, 210)                      # different head shape
    for cx in (114, 142):                           # different eyes
        eye = (xx - cx) ** 2 + (yy - 88) ** 2 < 9**2
        out[eye] = (240, 240, 20)
    out[112:118, 112:144] = (10, 10, 10)            # different mouth
    return out


def _neumorphic(img: np.ndarray) -> np.ndarray:
    """A plausible soft-shaded restyle: geometry kept, surface re-rendered."""
    from PIL import ImageFilter

    base = Image.fromarray(img)
    soft = np.asarray(base.filter(ImageFilter.GaussianBlur(1.5))).astype(np.float32)
    emboss = np.asarray(base.convert("L").filter(ImageFilter.EMBOSS)).astype(np.float32)
    out = soft * 0.78 + (emboss[..., None] - 128.0) * 0.55 + 34.0
    return np.clip(out, 0, 255).astype(np.uint8)


def _warp(img: np.ndarray, amplitude: float, period: float) -> np.ndarray:
    """Sinusoidal geometric mangling: the scene survives, the geometry does not."""
    import cv2

    h, w = img.shape[:2]
    grid_x, grid_y = np.meshgrid(
        np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32)
    )
    map_x = grid_x + amplitude * np.sin(2 * np.pi * grid_y / period)
    map_y = grid_y + amplitude * np.cos(2 * np.pi * grid_x / period)
    return cv2.remap(
        img, map_x.astype(np.float32), map_y.astype(np.float32),
        cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE,
    )


def _wrong_scene() -> np.ndarray:
    """A different set, a different character — the backend lost the plot."""
    img = np.zeros((SIZE, SIZE, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    img[..., 0] = (200 - xx * 0.4).astype(np.uint8)
    img[..., 1] = (30 + xx * 0.5).astype(np.uint8)
    img[..., 2] = (150 + yy * 0.2).astype(np.uint8)
    img[:70, :] = (18, 18, 24)                       # ceiling band
    img[40:130, 150:236] = (250, 240, 60)            # bright block, stage-right
    tri = (yy > 150) & (yy < 240) & (np.abs(xx - 70) < (yy - 150) * 0.7)
    img[tri] = (30, 40, 200)                         # a cone, not a figure
    ring = np.abs((xx - 190) ** 2 + (yy - 190) ** 2 - 44**2) < 700
    img[ring] = (255, 255, 255)
    return img


def _flicker(img: np.ndarray) -> np.ndarray:
    """Same geometry, whole surfaces re-rendered differently.

    This is what temporal fidelity is FOR, and it is what restyle flicker
    actually looks like: the backend re-decides the shading of every flat region
    frame to frame while the drawing underneath stays put. Real motion is
    cancelled by warping along the optical flow, so a fixture that merely MOVED
    would (correctly) score well — only unexplainable surface churn survives.
    """
    rng = np.random.default_rng(20260909)
    out = img.astype(np.float32)
    # Shimmer every flat region, the way a per-frame restyle would.
    quant = (img.astype(np.int32) // 24).sum(axis=2)
    for level in np.unique(quant):
        region = quant == level
        out[region] += rng.uniform(-46, 46, size=3)
    # Plus a low-frequency lighting wobble across the whole frame.
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    out += (18.0 * np.sin(2 * np.pi * (xx + yy) / 61.0))[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def _save(case: str, name: str, img: np.ndarray) -> None:
    d = FIXTURES / case
    d.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(d / f"{name}.png")


def build() -> list[str]:
    src = _canvas()
    cases: list[str] = []

    def pair(case: str, restyled: np.ndarray) -> None:
        _save(case, "source", src)
        _save(case, "restyled", restyled)
        cases.append(case)

    # 1. A perfect restyle. Every metric at ceiling.
    pair("identical", src.copy())

    # 2. Colour completely changed, geometry untouched — the case LPIPS-on-edges
    #    exists to forgive.
    pair("recolored_only", _hue_rotate(src, 140.0))

    # 3. Geometry moved, colour untouched — the case LPIPS-on-edges must catch.
    pair("edge_shifted", _translate(src, 7, 5))

    # 4. Structure smeared away.
    pair("gaussian_blurred", _gaussian_blur(src, 4))

    # 5. Same scene, different character — an identity failure, not a
    #    structural one.
    pair("face_swapped", _swap_face(src))

    # 6. Underexposed but structurally intact.
    pair("low_light", _gamma(src, 2.2))

    # 7. A believable stylisation.
    pair("neumorphic", _neumorphic(src))

    # 8. Geometry mangled while the palette survives -> the composite must FAIL.
    pair("structure_destroyed", _warp(src, 14.0, 26.0))

    # 9. The backend produced a different scene entirely.
    pair("wrong_scene", _wrong_scene())

    # 10. Consecutive restyled frames, barely moving -> temporal fidelity high.
    _save("static_pair", "prev", src)
    _save("static_pair", "curr", _translate(src, 1, 0))
    cases.append("static_pair")

    # 11. Consecutive restyled frames, large real motion. Flow should cancel it:
    #     moving is not flickering, and TF must not punish it.
    _save("high_motion", "prev", src)
    _save("high_motion", "curr", _translate(_hue_rotate(src, 60.0), 38, 26))
    cases.append("high_motion")

    # 12. Consecutive restyled frames, geometry identical, surface unstable.
    #     This is genuine flicker -> temporal fidelity must drop.
    _save("flicker_pair", "prev", src)
    _save("flicker_pair", "curr", _flicker(src))
    cases.append("flicker_pair")

    return cases


if __name__ == "__main__":
    built = build()
    print(f"{len(built)} fixture cases written to {FIXTURES}:")
    for c in built:
        print(" ", c)
