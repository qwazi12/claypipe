# ClayPipe

Takes any source video and outputs a 9:16 vertical comparison: an AI-restyled
recreation on top, the original footage below, the **original audio untouched**,
a branded header bar, and captions.

The defining rule: **the AI restyles frames as IMAGES, never video.** Audio and
timing are never regenerated, only re-muxed — sync is guaranteed by
construction, then verified by hashing the audio stream.

See `SPEC.md` for the full brief and `memory.md` for current state and decisions.

## Status — Build Order step 1 of 6

Working today: the complete pipeline **offline**, with a free `dummy` backend.

| Step | What | State |
|------|------|-------|
| 1 | Skeleton + extract + assemble (DummyBackend) | **done** |
| 2 | Scoring (SSIM / LPIPS-on-edges / ID / TF) | not started |
| 3 | Retry policy + cost firewalls | not started |
| 4 | Human gates (canary + flag review HTML) | not started |
| 5 | `FalBackend` — real API, real spend | not started |
| 6 | CLI polish + README of a real run | not started |

Nothing in the repo can spend money yet: the only backend is `dummy`, and
`--backend fal` refuses with a clear error.

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` on `PATH` (`brew install ffmpeg`). Checked at startup;
  a missing binary is a hard failure with an install message.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Run the offline pipeline

```bash
RUN=$(claypipe intake assets/test_clip.mp4 --style clay --title "Test Clip")
claypipe batch    "$RUN"     # extract frames + audio, restyle every frame
claypipe assemble "$RUN"     # reassemble, re-mux original audio, render 9:16
claypipe status   "$RUN"
```

`assemble` writes `final_comparison.mp4` (1080×1920, H.264 CRF 18, yuv420p) and
a `qc_card.json`, and appends to a project-level `history.jsonl`.

## Tests

```bash
.venv/bin/python -m pytest
```

20 tests, fully offline. `assets/test_clip.mp4` is a synthetic `testsrc` + `sine`
clip generated on first run — no third-party footage is bundled. No test may
call a paid endpoint.

## Configuration

All thresholds, weights and prompts live in versioned YAML and are validated on
startup — never hardcoded, never silently defaulted.

- `styles.yaml` — style profiles (prompt, strength, colours), render geometry,
  output directory
- `weights.yaml` — fidelity-score weights and thresholds (consumed from step 2)

## Guarantees enforced in code

- **Audio is copied, never re-encoded.** The final file's audio packet payloads
  must hash identically to both the extracted track and the source video, or
  assembly fails.
- **Frame counts must agree:** extracted == restyled == reassembled == output.
- **No fabricated data.** QC-card fields that do not exist yet are `null`.
