# MEMORY — ClayPipe

## Current State
- **Build Order step 1 of 6 is COMPLETE and green.** The full pipeline runs
  end-to-end offline on the synthetic test clip: intake -> batch -> assemble.
- 20 tests pass in ~28s. Zero network calls, zero API spend, no credential needed.
- Nothing deployed anywhere (this is a local CLI, not a service). Nothing can
  spend money yet: `dummy` is the only backend and `--backend fal` refuses.
- SPEC.md is the complete brief, including the Addendum (A1–A5) decided 2026-09-08.

### Step 1 acceptance — verified, not assumed
| Check | Result |
|---|---|
| `pytest` green | 20 passed |
| output plays | full decode pass, `-xerror`, no errors |
| ffprobe 1080x1920 | 1080x1920, h264, yuv420p, 12fps |
| AAC bit-identical to source | MD5 `41fb16f4863d3ace1d5c88bd7a3b1585` on source video, extracted `audio.aac`, and `final_comparison.mp4` |
| frame invariant | extracted 60 == restyled 60 == reassembled 60 == output 60 |

## Environment (verified 2026-09-08)
- ffmpeg/ffprobe 8.1.2 at `/opt/homebrew/bin` — libass YES, libfreetype YES,
  **`drawtext` filter ABSENT** (Homebrew ffmpeg 8 ships without it).
- python3 3.13.2 at `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`.
- Project venv at `.venv` (gitignored).

## Decisions
- **D1 (2026-09-08) Header bar is a PIL-rendered PNG overlay, not `drawtext`.**
  SPEC §6 asks for `drawtext`; the installed ffmpeg has no such filter, which is
  the "hard blocker" escape hatch in the Tech Stack section. A 1080×N header PNG
  is composited with `overlay`. Verified, not assumed: `ffmpeg -filters | grep -w
  drawtext` → no match.
- **D2 (2026-09-08) Audio sync guarantee is a packet-level MD5 equality check.**
  `ffmpeg -i X -map 0:a -c copy -f md5 -` on the extracted `audio.aac` and on
  `final_comparison.mp4`; mismatch is a hard fail (SPEC Hard Rules).
- **D3 (2026-09-08) Dependencies are staged per build step.** Only step-1 deps
  (typer/pydantic/PyYAML/Pillow/pytest) are installed; scoring (lpips/torch/
  scikit-image), scenedetect, faster-whisper and fal are declared as optional
  extras and installed when their build step lands. Keeps step 1 fast and offline.
- **D4 (2026-09-08) Modules for later steps are NOT stubbed out.** `score.py`,
  `retry.py`, `captions.py`, `shots.py`, `review/` are created in their own steps,
  not as empty files now.
- **D5 (2026-09-08) The 9:16 canvas is built with `pad`, never a `color` source.**
  A colour generator is an INFINITE input; overlaying finite panels onto one
  renders until the disk fills. It did exactly that on the first run (46 MB and
  climbing before it was killed). Padding a finite input bounds the whole graph.
  `ffmpeg.run` is also time-boxed (900s) so a runaway can never repeat silently.
- **D6 (2026-09-08) The output frame count is bounded by a `trim` filter, NOT
  `-frames:v`.** The original panel runs at a higher rate and is fractionally
  longer, so the render trailed 2 extra frames past the restyled sequence.
  `-frames:v` fixes that but finishes the mux early and TRUNCATES THE AUDIO,
  which voids the bit-identity guarantee — caught by the hash check. The bound
  belongs in the filtergraph. Regression test:
  `test_output_frame_count_matches_restyled_sequence`.
- **D7 (2026-09-08) AAC hashes are normalised with `aac_adtstoasc`.** The same
  audio carries a 7-byte ADTS header per frame in a raw `.aac` and none inside
  MP4, so a naive packet hash reports a difference that is pure framing (453 vs
  460 bytes on frame 1, identical 236-packet counts). The bsf strips ADTS headers
  and is a verified no-op on MP4. Without this the sync check false-alarms.

## Pending / Next
- **A4 — BLOCKED ON OPERATOR: the Google Drive folder ID is TBD.** Do not invent
  one. `output.drive_folder_id` in `styles.yaml` stays `null` until the operator
  supplies it. It decides whether ClayPipe output feeds an existing brand's
  round-robin or needs a new sheet tab in SocialPilot AI.
- Step 2 — scoring module (SSIM / LPIPS-on-edges / ID / TF). Await operator GO.
- Step 3 — retry + cost firewalls, incl. A3 cumulative PROJECT-level spend cap
  (the per-run `--max-cost-usd` alone does not stop ten aborted runs costing 10×).
- Step 4 — human gate HTML pages; Step 5 — FalBackend; Step 6 — CLI polish + README.
- A2 — Flux Kontext vs SDXL+ControlNet stays deferred until after the first real
  canary. Do not pre-optimize.
- Captions (Whisper → SRT) are not built yet; `assemble` already burns in an SRT
  if one is present at `subs.srt`, so step-6 wiring is a drop-in.
- RUNBOOK.md / CONFIG.md (Rule 33) not written yet — due with step 6.

## Log (append-only, newest first)

### 2026-09-08 — Step 1 shipped: offline pipeline end-to-end, first commit
- Built: `config.py` (pydantic + YAML, validated on startup), `ffmpeg.py`
  (startup check, timeout, packet hashing), `logging.py` (JSONL + runId),
  `run.py` (run dir + manifest), `pipeline/{extract,restyle,assemble,qccard}.py`,
  `cli.py` (intake/batch/assemble/status/version), 20 tests, README.
- `assets/test_clip.mp4` generated synthetically per A1 (testsrc + sine, 5s,
  1280x720, AAC 48kHz mono) — no third-party footage bundled.
- Three real defects found and fixed during the step, each now covered by a test
  or a guard: the infinite `color` source (D5), the `-frames:v` audio truncation
  (D6), and the ADTS framing false-alarm (D7).
- SPEC §6 asks for `drawtext`; this ffmpeg has no such filter, so the header is a
  PIL PNG overlay (D1). This is the one deliberate deviation from the spec text.
- Step 2 NOT started — awaiting operator confirmation, per instruction.

### 2026-09-08 — Session start: spec read, memory created, step 1 begun
- Read SPEC.md in full (262 lines) including Addendum A1–A5.
- Verified environment before writing code (ffmpeg filters, fonts, python).
- Created this file per Rule 0/P1. No code written before it existed.
- Scope for this session: Build Order step 1 ONLY (skeleton + extract + assemble
  with DummyBackend). Step 2 not to start until step 1 acceptance passes AND the
  operator confirms.
