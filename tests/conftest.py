"""Test fixtures. Nothing here touches the network or a paid API (Rule 32)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_CLIP = REPO_ROOT / "assets" / "test_clip.mp4"

CLIP_SECONDS = 5
CLIP_SIZE = "1280x720"


def _generate_test_clip(dst: Path) -> None:
    """Synthetic 5s clip: testsrc video + sine audio (SPEC A1).

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
