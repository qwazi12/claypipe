# ClayPipe

Takes any source video and outputs a 9:16 vertical comparison: an AI-restyled
recreation on top, the original footage below, the **original audio untouched**,
a branded header bar, and captions.

The defining rule: **the AI restyles frames as IMAGES, never video.** Audio and
timing are never regenerated, only re-muxed — sync is guaranteed by
construction, then verified by hashing the audio stream.

See `SPEC.md` for the full brief and `memory.md` for current state and decisions.

## Status

The pipeline runs end to end **offline** today. Scoring, the retry policy and
all five cost firewalls are live. The paid backend is wired but stubbed.

| Step | What | State |
|------|------|-------|
| 1 | Skeleton + extract + assemble (DummyBackend) | done — `a0dcba9` |
| 2 | Scoring (SSIM / LPIPS-on-edges / ID / TF) | done — `c30dfe2` |
| 3 | Retry policy + cost firewalls | done — `cae2628` |
| 4 | Human gates (canary + flag review HTML) | done — `404e43c` |
| 5 | Gate + scorer wiring, canary CLI, snapshot export | done — `4d63767`, `8dc3512`, `f4dcf3b`, `6f9421a` |
| 5 | `FalBackend` — real API, real spend | **stubbed**; no live call has ever been made |
| 6 | CLI polish + docs of a real run | done — see `docs/REAL_RUN.md` |

Nothing in the repo has ever spent money. `--backend fal` refuses unless BOTH
`--live` is passed and `FAL_KEY` is set, and the backend itself is a stub that
raises rather than calling an endpoint.

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` on `PATH` (`brew install ffmpeg`). Checked at startup;
  a missing binary is a hard failure with an install message.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Paid backends

`--backend fal` needs **two** things, and refuses loudly without either:

```bash
export FAL_KEY=...                      # the capability
claypipe batch "$RUN" --backend fal --live   # the intent
```

`--live` exists so a paid run cannot happen by accident — not from a stale
shell export, not from a config file someone forgot about. Never pass a key as
a command-line argument; it lands in your shell history. `.env` is gitignored.

Install the client only when you need it: `pip install -e '.[fal]'`.

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

## What this CLI does **not** do

* **No web server.** The review pages are single self-contained files opened
  from `file://`; verdicts come back as a form-encoded URL you paste to
  `claypipe canary submit`. Nothing listens on a port.
* **No API call without two locks.** `--backend fal` needs `--live` *and*
  `FAL_KEY`. The backend is currently a stub that raises rather than calling an
  endpoint; no fal.ai call has ever been made from this repo.
* **No automatic cross-clip iteration.** One clip, one run. Tuning across clips
  is the operator reading `history.jsonl`, not the pipeline adjusting itself.
* **No cross-repo integration.** ClayPipe writes `final_comparison.mp4` into its
  output directory and stops. Drive uploads, schedulers and social posting live
  in other repos (`memory.md` D8).
* **No captions yet.** `assemble` burns in `subs.srt` when one is present;
  nothing generates it yet.

See `docs/REAL_RUN.md` for a verbatim end-to-end transcript.

## Guarantees enforced in code

- **Audio is copied, never re-encoded.** The final file's audio packet payloads
  must hash identically to both the extracted track and the source video, or
  assembly fails.
- **Frame counts must agree:** extracted == restyled == reassembled == output.
- **No fabricated data.** QC-card fields that do not exist yet are `null`.
