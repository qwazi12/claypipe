"""Test fixtures. Nothing here touches the network or a paid API (Rule 32)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Hard-lock the test process offline for model hubs. Learned-metric tests use
# the local cache or skip; they never download. Set before any torch/open_clip
# import so the flag is in place when a hub client is first constructed.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_CLIP = REPO_ROOT / "assets" / "test_clip.mp4"

# 8 seconds, not 5, because the architecture's smallest purchasable unit is
# VACE's 81-frame minimum = 6.75s at 12fps. A 5-second clip is 60 frames and
# cannot be canaried OR batched on a v2v backend at all, so a suite built on
# one could never exercise the path it is meant to cover.
CLIP_SECONDS = 8
CLIP_SIZE = "1280x720"


def _generate_test_clip(dst: Path) -> None:
    """Synthetic clip: testsrc video + sine audio (SPEC A1).

    Deterministic, tiny, and legally unencumbered — no bundled real footage.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={CLIP_SIZE}:rate=30:duration={CLIP_SECONDS}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={CLIP_SECONDS}",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-shortest", str(dst)],
        check=True, capture_output=True,
    )


@pytest.fixture(scope="session")
def test_clip() -> Path:
    """The bundled test clip, generated on first use if absent."""
    if not TEST_CLIP.is_file():
        _generate_test_clip(TEST_CLIP)
    return TEST_CLIP
