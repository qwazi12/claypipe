"""Extraction resumability is decided by a sentinel, not by a frame count.

DEFENDS: the silent-short-clip failure. A directory with frames in it proves
only that ffmpeg started; a run killed partway leaves a plausible-looking
directory that a count-based check accepts, and the pipeline then builds a
clip that is quietly missing its tail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claypipe.logging import RunLogger
from claypipe.pipeline.extract import (
    SENTINEL_NAME,
    count_frames,
    extract_frames,
    read_sentinel,
)

FPS = 12
EXPECTED = 60


@pytest.fixture
def logger(tmp_path: Path) -> RunLogger:
    return RunLogger("sentinel-test", tmp_path / "logs" / "run.jsonl", echo=False)


def test_extract_writes_sentinel_only_on_complete(
    tmp_path: Path, test_clip: Path, logger: RunLogger
) -> None:
    """DEFENDS: a partial extraction being mistaken for a finished one."""
    out = tmp_path / "frames" / "source"

    count = extract_frames(test_clip, out, FPS, logger)
    assert count == EXPECTED

    # The file name is asserted literally so a refactor that renames it fails
    # here rather than silently disabling resumability.
    sentinel_path = out / ".extract_complete"
    assert sentinel_path.is_file()
    assert sentinel_path.name == SENTINEL_NAME

    sentinel = json.loads(sentinel_path.read_text())
    assert sentinel["frames"] == EXPECTED
    assert sentinel["fps"] == FPS
    assert sentinel["extracted_at"].endswith("Z")
    assert len(sentinel["source_sha256"]) == 64

    # A matching sentinel means the second call is a no-op reuse.
    first_mtime = sorted(out.glob("f_*.png"))[0].stat().st_mtime_ns
    assert extract_frames(test_clip, out, FPS, logger) == EXPECTED
    assert sorted(out.glob("f_*.png"))[0].stat().st_mtime_ns == first_mtime

    # Removing the sentinel forces a re-extract even though frames are present.
    sentinel_path.unlink()
    assert extract_frames(test_clip, out, FPS, logger) == EXPECTED
    assert (out / SENTINEL_NAME).is_file()
    assert sorted(out.glob("f_*.png"))[0].stat().st_mtime_ns != first_mtime


def test_frames_without_a_sentinel_are_re_extracted_and_recorded(
    tmp_path: Path, test_clip: Path, logger: RunLogger
) -> None:
    """DEFENDS: a truncated extraction producing a short final video.

    The half-finished directory is discarded, an incident note records what was
    found, and extraction runs again — never 'close enough'.
    """
    run_dir = tmp_path / "run"
    out = run_dir / "frames" / "source"
    out.mkdir(parents=True)
    for i in range(1, 18):  # a plausible-looking partial extraction
        (out / f"f_{i:05d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert count_frames(out) == 17

    count = extract_frames(test_clip, out, FPS, logger, incident_dir=run_dir)

    assert count == EXPECTED, "the partial extraction must not be reused"
    incidents = list((run_dir / "incidents").glob("*-incomplete_extract.json"))
    assert len(incidents) == 1
    note = json.loads(incidents[0].read_text())
    assert note["frames_found"] == 17
    assert note["expected_sentinel"] == SENTINEL_NAME


def test_a_changed_source_invalidates_the_sentinel(
    tmp_path: Path, test_clip: Path, logger: RunLogger
) -> None:
    """DEFENDS: reusing frames that belong to a DIFFERENT video.

    The frame count would match, so only the source hash catches this.
    """
    out = tmp_path / "frames" / "source"
    extract_frames(test_clip, out, FPS, logger)

    sentinel = json.loads((out / SENTINEL_NAME).read_text())
    sentinel["source_sha256"] = "0" * 64
    (out / SENTINEL_NAME).write_text(json.dumps(sentinel))

    first_mtime = sorted(out.glob("f_*.png"))[0].stat().st_mtime_ns
    extract_frames(test_clip, out, FPS, logger)
    assert sorted(out.glob("f_*.png"))[0].stat().st_mtime_ns != first_mtime
    assert json.loads((out / SENTINEL_NAME).read_text())["source_sha256"] != "0" * 64


def test_read_sentinel_survives_a_corrupt_file(tmp_path: Path) -> None:
    """A corrupt sentinel is 'no sentinel', never a parse crash mid-run."""
    out = tmp_path / "frames"
    out.mkdir()
    assert read_sentinel(out) is None
    (out / SENTINEL_NAME).write_text("{not json")
    assert read_sentinel(out) is None
