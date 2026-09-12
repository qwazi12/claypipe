# PROJECT: ClayPipe — Automated AI Recreation Comparison Video Pipeline

> weights.yaml is authoritative. SPEC.md documents intent; values live in YAML.

## Mission

Build a production-grade CLI system that takes any source video as input and outputs
a 9:16 vertical comparison video: an AI-restyled recreation (claymation / LEGO /
logo-animation style) on the TOP panel, the ORIGINAL footage on the BOTTOM panel,
with the ORIGINAL audio track untouched, a branded header bar, and auto-generated
captions. Reference style: TikTok creator @trevorcarlee's split-screen LEGO
recreations of TV scenes.

The defining architectural rule: **the AI restyles frames as IMAGES, never video.**
Audio and timing are never regenerated — only re-muxed. Sync is guaranteed by
construction, not checked after the fact.

---

## Core Architecture

```
INPUT VIDEO (mp4/mov/mkv)
  │
  STAGE 0  INTAKE      → human locks style prompt + character reference images
  STAGE 1  CANARY      → restyle 3 representative frames, score them,
                         human GO/NO-GO before any batch spend
  STAGE 2  BATCH       → extract frames @12fps, detect shots, AI-restyle each
                         frame, score each frame, auto-retry failures (max 2×)
  STAGE 3  FLAG REVIEW → human reviews outliers + 2% random audit sample
  STAGE 4  ASSEMBLY    → reassemble frames, re-mux ORIGINAL audio, build 9:16
                         stacked comparison with header + captions
  STAGE 5  FINAL WATCH → human 1× viewing, binary ship/reject
  STAGE 6  LOG         → write QC card JSON to project record
```

---

## Tech Stack (use exactly this unless a hard blocker exists)

- **Language:** Python 3.11+, `typer` for the CLI, `pydantic` for config/models
- **Video I/O:** `ffmpeg`/`ffprobe` via subprocess (system binary, add a startup
  check with a clear install error message)
- **Shot detection:** `scenedetect[opencv]` (ContentDetector, threshold 27)
- **AI restyle:** `fal-ai` client library. Primary endpoint: Flux Kontext
  (image edit — best structure fidelity). Fallback endpoint: SDXL img2img with
  ControlNet depth + lineart. Abstract the backend behind a `RestyleBackend`
  protocol so DomoAI or Replicate can be added later without touching the pipeline.
- **Scoring:** `scikit-image` (SSIM), `lpips` (perceptual distance on EDGE MAPS,
  not raw color), `opencv-python` (optical flow for flicker), `insightface` or
  `open_clip` (character identity embeddings)
- **Captions:** `faster-whisper` (base model, word timestamps → SRT)
- **QC persistence:** JSONL append log + per-clip `qc_card.json`
- **Testing:** `pytest`, with a bundled 5-second test clip and a `dummy` backend
  (PIL-based cartoonize) so the ENTIRE pipeline is testable offline with zero
  API spend

---

## Detailed Requirements

### 1. Frame extraction
- Extract frames at configurable `--fps` (default 12 — stop-motion cadence AND
  halves AI cost vs 24fps)
- Extract the audio track once, losslessly, to `audio.aac`. It is read-only
  for the rest of the run.
- Zero-padded sequential naming `f_00001.png` so frame order is trivially
  recoverable. Frame count must be verified at assembly: extracted == restyled
  == reassembled. Any mismatch = hard fail.

### 2. Shot-aware restyle with anti-drift controls
- Run scene detection first; process each shot independently.
- **Fixed seed per shot** (seed = 1000 + shot_index) to kill cross-shot drift.
- Moderate img2img strength/denoise (~0.65, configurable per style profile).
- Style prompts live in a `styles.yaml`:
  - `clay`: "claymation, plasticine clay stop-motion, fingerprints in clay,
    handmade miniature set, Aardman style, keep exact same composition, pose,
    camera angle, framing and colors"
  - `lego`: "LEGO minifigure stop-motion, glossy plastic bricks, LEGO movie
    style, brick-built environment, keep exact same composition, pose, camera
    angle, framing and colors"
  - `logo`: "flat 2D logo-style motion graphic, bold shapes, brand colors,
    keep exact same composition and framing"
- Resume-safe: skip frames whose output already exists (crashed runs must be
  resumable without re-spending).

### 3. Accuracy scoring (the heart of the system)

Implement this exact composite Fidelity Score per frame:

```
F = 0.40·SSIM + 0.25·(1 − LPIPS_edges) + 0.20·ID + 0.15·TF

  SSIM        structural similarity, restyled vs source frame       target ≥ 0.72
  LPIPS_edges perceptual distance computed on Canny edge maps of both
              images — NEVER on raw color, because a correct restyle
              SHOULD differ in color/texture; geometry should not   target ≤ 0.35
  ID          cosine similarity of character embedding vs. the
              human-locked reference from Stage 0                   target ≥ 0.85
  TF          temporal consistency: 1 − normalized optical-flow-warped
              difference between consecutive restyled frames        target ≥ 0.80

  PASS       F ≥ 0.75  → auto-accept
  BORDERLINE 0.65–0.75 → auto-retry once with strength −0.10; if still
                         borderline, add to human review queue
  FAIL       F < 0.65  → auto-retry with new seed; after 2 failures,
                         add to human review queue
```

- Weights must live in config (`weights.yaml`), not code — the FORMULA is fixed,
  weights are per-project tuning.
- Score every frame. Store per-frame scores to `scores.jsonl`.

### 4. Cost firewalls (hard circuit-breakers, not warnings)
1. **Canary gate:** batch mode REFUSES to run unless a canary (3 frames: first,
   middle, most-motion shot) has been scored and a human has written
   `approved: true` to `canary_verdict.json`.
2. **Retry budget:** max 2 retries per frame AND total retries capped at 15% of
   frame count. Breach → pause run, write incident note, wait for human.
3. **Kill switch:** after the first 50 batch frames, if mean F < 0.70, auto-abort
   with a report. Never discover a bad run at frame 700.
4. Track cumulative API spend per run and per project; `--max-cost-usd` flag that
   halts the run when exceeded (estimate cost per frame from config).

### 5. Human gates (CLI-driven, explicit)
- `claypipe intake <video>` — interactive: pick style, register character
  reference images, generates the run config
- `claypipe canary <run>` — produces 3 restyled frames + scores, opens an
  HTML review page, blocks until verdict file written
- `claypipe batch <run>` — the main automated stage
- `claypipe review <run>` — HTML review page of flagged frames + 2% random
  audit sample; reviewer clicks accept/retry/reject per frame
- `claypipe assemble <run>` — final render
- `claypipe status <run>` — live score/cost/progress dashboard (terminal)

The review HTML pages must be self-contained single files (inline base64
thumbnails), opened via `webbrowser`, and write their verdicts to JSON that the
pipeline polls for. No web server.

### 6. Assembly
- Reassemble restyled frames at the extraction fps; mux the ORIGINAL `audio.aac`
  with `-c:a copy` (bit-for-bit — this is the sync guarantee; add a verification
  step that hashes source audio vs output audio stream and hard-fails on mismatch)
- Build the 9:16 (1080×1920) comparison with one ffmpeg filter_complex:
  crop the central action band from each panel source (preserve aspect),
  scale each panel to 1080×~882, lay over a colored background
  (default `#F2C230` yellow; per-style configurable), drawtext header bar with
  the clip title, and burn in the Whisper SRT captions centered on the divider
- Output: `final_comparison.mp4`, H.264, CRF 18, yuv420p

### 7. QC Card (every clip, always)
```json
{
  "clip_id": "...", "style": "lego", "fps": 12, "frames": 744,
  "scores": {"mean_F": 0.81, "min_F": 0.58, "p5_F": 0.71},
  "auto_retries": 41, "human_flags": 12, "human_overrides": 3,
  "cost_usd": {"canary": 0.12, "batch": 21.40, "retries": 2.20, "total": 23.72},
  "verdict": "shipped",
  "lessons": "free-text field the human fills at Stage 5"
}
```
Append to a project-level `history.jsonl` so per-style tuning insights accumulate.

---

## Repo Layout

```
claypipe/
  pyproject.toml
  styles.yaml
  weights.yaml
  claypipe/
    cli.py            # typer commands: intake/canary/batch/review/assemble/status
    pipeline/
      extract.py      # ffmpeg frame+audio extraction
      shots.py        # scene detection
      restyle.py      # RestyleBackend protocol + FalBackend + DummyBackend
      score.py        # SSIM/LPIPS/ID/TF + composite F
      retry.py        # retry policy + budgets + kill switch
      captions.py     # whisper → SRT
      assemble.py     # reassembly + 9:16 stack + audio hash verification
      qccard.py
    review/
      canary_page.py  # self-contained HTML review generators
      flag_page.py
  tests/
    test_pipeline_e2e.py   # uses DummyBackend + bundled 5s clip, zero API spend
    test_score.py
    test_retry.py
  assets/test_clip.mp4
```

---

## Build Order & Acceptance Criteria

Build in this order, running tests after each step:

1. **Skeleton + extract + assemble with DummyBackend** — the full pipeline must
   run end-to-end OFFLINE on the test clip and produce a valid 9:16 mp4 with
   original audio. Acceptance: `pytest` green; output plays; `ffprobe` shows
   1080×1920 and an AAC stream bit-identical to source audio.
2. **Scoring module** — unit tests on synthetic frame pairs (identical frames →
   F ≈ 1.0; heavily distorted → F low; edge-shift-only → LPIPS_edges catches it,
   recolor-only → LPIPS_edges does NOT fail it).
3. **Retry + firewalls** — unit tests proving: retry caps enforced, kill switch
   aborts at frame 50 when mean F < 0.70, canary gate blocks batch without verdict.
4. **Human gates** — canary and flag review HTML pages generate and round-trip
   verdicts.
5. **FalBackend** — real API integration, behind `--backend fal`, requiring
   `FAL_KEY` env var. Do NOT call it in tests.
6. **CLI polish + README** — document one complete real-clip run.

## Hard Rules

- NEVER regenerate or re-encode the audio. Copy only. Verify by stream hash.
- NEVER let batch run without a human canary verdict.
- NEVER score LPIPS on raw color images — edge maps only.
- Every API call must be logged with cost to the spend ledger before execution.
- All thresholds/weights/prompts in YAML config, never hardcoded.
- If any acceptance check fails, stop and report — do not silently work around it.

---

## Addendum (decided 2026-09-08)

### A1. Test clip
No test clip is bundled yet. Generate a synthetic 5-second clip with ffmpeg
(`testsrc` + `sine` audio) into `assets/test_clip.mp4` as part of step 1, OR use
any short mp4 dropped into `assets/`. The DummyBackend makes the whole pipeline
testable with zero API spend.

### A2. Open decision — deliberately deferred
Which fal.ai endpoint (Flux Kontext vs. SDXL+ControlNet) is a QUALITY decision to
make after the first real canary. The `RestyleBackend` protocol means it can be
swapped without rebuilding anything. Do not pre-optimize this in steps 1–4.

### A3. Project-level spend cap (addition to §4.4)
`--max-cost-usd` as specified is PER-RUN. Ten aborted runs at $19 each still cost
$190. Also track a cumulative PROJECT-level spend total in the same ledger, with
its own configurable cap. Both caps are hard halts.

### A4. Integration contract with SocialPilot AI
ClayPipe is the PRODUCTION stage; the existing SocialPilot AI repo
(`~/dev/dungeon-odyssey-review/socialpilot_Ai`) is the DISTRIBUTION stage. They stay
separate repos. The ENTIRE integration surface is one Google Drive folder:

    ClayPipe writes final_comparison.mp4  →  a Drive folder
                                          →  SocialPilot's update_sheet_smart.js
                                             already round-robin-ingests Drive folders

No code coupling, no shared library, no imports across repos. Add a configurable
output directory (and optionally a Drive folder ID) so the finished file lands in
the agreed folder. The Drive folder ID is TBD — ask the operator before wiring it.

### A5. Operating rules
Global Rules 0–40 (`~/.claude/CLAUDE.md`) apply and load automatically. Of note here:
- P1: create `memory.md` before writing code; P4: read it first every session.
- Rule 25: all thresholds/weights/prompts in versioned YAML, validated on startup.
- Rule 30: this pipeline spends real money — dry-run, scope limits, and
  confirmations are the cost firewalls in §4. They are not optional.
- Rule 34: every run gets a `runId`, propagated through logs, scores, and the QC card.
- Rule 32: steps 1–4 must be fully testable offline. Never call fal.ai in a test.
