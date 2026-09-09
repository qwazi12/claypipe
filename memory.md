# MEMORY — ClayPipe

## Current State
- **Steps 1 and 2 of 6 are COMPLETE and green.** Step 1: the pipeline runs
  end-to-end offline (intake -> batch -> assemble). Step 2: `pipeline/score.py`
  implements the composite F score over 11 synthetic fixture cases.
- 76 tests pass in ~33s with model weights warm; 43 pass / 33 skip / 0 fail on a
  cold clone (learned-metric tests skip rather than download). Zero network calls
  during tests, zero API spend, no credential needed.
- **TWO DESIGN FINDINGS ARE OPEN AND NEED AN OPERATOR DECISION — see D9 and D10.**
  Step 3 should not be built until they are settled, because the retry policy is
  driven by exactly the verdicts these findings say are under-triggering.
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
- **D1 (2026-09-08, OPERATOR-APPROVED 2026-09-09) Header bar is a PIL-rendered
  PNG overlay, not `drawtext`.** SPEC §6 asks for `drawtext`; Homebrew's ffmpeg 8
  ships without it (no harfbuzz), which is the "hard blocker" escape hatch in the
  Tech Stack section. A 1080×N header PNG is composited with `overlay`. Verified,
  not assumed: `ffmpeg -filters | grep -w drawtext` → no match.
  **This is what we ship.** Do NOT "fix" it by reinstalling or pinning a
  different ffmpeg build — the Pillow path is the approved design, not a
  workaround awaiting repair. Caption burn-in is unaffected: libass IS present,
  so SRT burn-in stays on the `subtitles=` filter.
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
- **D8 (2026-09-09, OPERATOR-DIRECTED) Cross-repo integration deferred;
  ClayPipe's output dir is the contract surface.** `output.drive_folder_id` and
  its null-gate test are DELETED from `styles.yaml`, `config.py`, `run.py` and
  `tests/test_config.py`. ClayPipe produces `final_comparison.mp4` and stops.
  Drive uploads, SocialPilot round-robin, schedulers and social hooks belong to
  other repos.
  **AUTHORITY CONFLICT — read this before following SPEC.md.** The field was not
  invented last session: SPEC.md Addendum A4 explicitly instructs adding it
  ("Add a configurable output directory (and optionally a Drive folder ID)").
  The operator's 2026-09-09 build brief reverses that decision. **The build brief
  supersedes SPEC.md A4.** SPEC.md was left unedited (changing it was not
  authorised), so a future session reading SPEC.md alone WILL be tempted to
  re-add this. Do not. If it seems needed, surface the choice to the operator.
  (For the record: the field was a plain `null` YAML entry with a comment — it
  was never encrypted or obfuscated, contrary to the brief's §14 characterisation.
  Nothing was stored, and no ID was ever invented.)
- **D9 (2026-09-09) FINDING — the composite F cannot gate identity or temporal
  drift. CLOSED 2026-09-09 by Decision 1, see D13.** With the specified weights
  (0.40/0.25/0.20/0.15), take a frame perfect in every other respect and drive
  ONE component to its worst possible value:
  | component at worst | F | verdict |
  |---|---|---|
  | SSIM = 0 | 0.600 | FAIL |
  | LPIPS_edges = 1 | 0.750 | PASS (exactly on the line) |
  | ID = 0 | 0.800 | PASS |
  | TF = 0 | 0.850 | PASS |
  Only SSIM can single-handedly trigger FAIL. So brief §9 failure modes 2
  (temporal flicker) and 3 (identity drift) — both listed as "prevented at
  BATCH" — are NOT gated by the composite at all. Shown on a real fixture:
  `structure_destroyed` has mangled geometry and ID 0.653 against a 0.85 target,
  and still scores F=0.814 PASS. The per-component `targets_met` dict catches
  it; the verdict ignores `targets_met` entirely.
  Formula left EXACTLY as specified — not silently patched. Pinned by
  `test_finding_identity_or_temporal_alone_cannot_drop_below_pass` and
  `test_finding_per_component_targets_catch_what_the_composite_misses`.
  Options put to the operator: (a) leave F alone and make step 3 treat any
  missed component target as a flag/retry trigger independent of F;
  (b) re-weight; (c) redefine PASS as "F >= 0.75 AND all component targets met".
- **D10 (2026-09-09) FINDING — the TF target of 0.80 is nearly unbreachable.
  CLOSED 2026-09-09 by Decision 2, see D14.** TF is `1 - mean(|flow-warped prev - curr|)/255`
  over the WHOLE frame, so it is dominated by the unchanged majority of the
  frame and saturates near 1. Breaching TF < 0.80 requires consecutive frames
  differing by an average of >51 grey levels after motion compensation, which is
  catastrophic rather than flickery. Measured: still pair 0.997, large real
  motion 0.932, severe whole-surface shimmer 0.916 — the shimmer fixture sits
  comfortably inside a target meant to catch it.
  The RANKING is correct (flicker < motion < static), so the metric is
  directionally sound; only its sensitivity and threshold are wrong.
  Options: (a) keep the mean and treat TF as a catastrophe detector only;
  (b) use a high percentile (p95/p99) of the warped difference instead of the
  mean — config change plus a one-line formula change inside the metric;
  (c) raise the TF target from 0.80 to ~0.95 — pure config, no code change.
  Pinned by `test_finding_temporal_target_is_hard_to_breach`.
- **D11 (2026-09-09) open_clip must use `ViT-B-32-quickgelu`, not `ViT-B-32`.**
  The `openai` checkpoint was trained with QuickGELU activations. Pairing it with
  the plain-GELU architecture makes open_clip emit a WARNING and proceed,
  silently producing embeddings the checkpoint was never trained to emit.
  Caught from that warning during step 2. Measured effect on identity: up to
  +0.035 on the face-swap fixture. Corrected in `weights.yaml`.
- **D12 (2026-09-09) Identity scoring requires Stage-0 references and hard-fails
  without them.** ID carries weight 0.20 in a fixed formula, so there is no
  defined score with no reference. `insightface` was rejected in favour of
  `open_clip`: insightface is a HUMAN FACE recogniser and will frequently detect
  no face at all on a claymation or LEGO character, which is the exact material
  this pipeline produces. **OPEN QUESTION for the operator:** the `logo` style
  has no character at all, so it currently cannot be scored. Decide before logo
  clips run.
- **D13 (2026-09-09) Learned-metric model weights are warmed deliberately, never
  downloaded during a run or a test.** `tests/conftest.py` sets
  `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`; `score.models_are_cached()` is a
  pure predicate and the LPIPS/identity tests skip when the cache is cold.
  Verified: cold clone gives 43 passed / 33 skipped / 0 failed.
  macOS note: the first warm-up needs `SSL_CERT_FILE=$(python -c "import certifi;
  print(certifi.where())")` — the python.org framework build ships no CA bundle
  and the download fails with CERTIFICATE_VERIFY_FAILED without it.
- **D13 (2026-09-09, OPERATOR DECISION 1) PASS = `F >= 0.75` AND all four
  component targets met.** Closes D9. `weights.yaml` gains
  `thresholds.require_component_targets: true`; the formula is untouched.
  Gate order, as implemented in `score.classify()` and documented in
  `weights.yaml`:
    0. FAIL is a composite-only veto, settled FIRST: `F < borderline` -> FAIL.
       A component miss must never be what FAILs a frame.
    1. Component targets next. Any miss -> BORDERLINE (`targets_missed`).
    2. Composite F last. `borderline <= F < pass` -> BORDERLINE
       (`composite_borderline`).
  The operator's brief said "targets first, then F"; taken literally that would
  make a target miss at F=0.2 a BORDERLINE, contradicting the F<0.65 -> FAIL
  band. Resolved as above — targets first among the NON-FAIL outcomes — which
  satisfies both statements. A test caught the ordering error before it shipped
  (`test_decision1_component_miss_alone_is_never_fail`).
  `FrameScore` now carries a `reason` (`VerdictReason`) so retry.py can branch
  on WHY, not just on the verdict.
- **D14 (2026-09-09, OPERATOR DECISION 2) `targets.tf_min` raised 0.80 -> 0.95.**
  Closes D10. Pure config; no code touched, metric unchanged.
- **D15 (2026-09-09) FINDING — tf_min=0.95 flags legitimate fast motion, not
  just flicker. OPEN, not blocking.** Measured: real motion 0.932, severe
  shimmer 0.916 — **0.016 apart**. No single threshold separates them, so any
  target that catches the flicker also catches genuine fast action. Consequence:
  action-heavy shots will generate human-review load at Stage 3. Decision 2 was
  applied verbatim as directed; this records the cost rather than absorbing it.
  Pinned by `test_finding_d15_tf_target_also_flags_legitimate_fast_motion`.
  If review load proves excessive, the lever is a motion-aware TF (e.g. mask
  pixels where flow magnitude is high) — a code change, so operator decision.
- **D16 (2026-09-09) Two fixtures beyond `structure_destroyed` moved
  PASS -> BORDERLINE under Decision 1, and one under Decision 2.** The operator
  brief predicted only `structure_destroyed` would move and that "all other
  previously-passing fixtures stay PASS". Actual: `low_light` (SSIM 0.521 vs
  0.72), `gaussian_blurred` (ID 0.845 vs 0.85 — misses by 0.005), and
  `high_motion` (TF 0.932 vs the new 0.95) also moved. All three were ALREADY
  missing a component target before Decision 1; the decision simply made that
  consequential, which is what it was for. Not a defect — but the acceptance
  bullet as written could not be met, so it is recorded rather than glossed.
  Note `gaussian_blurred` sits 0.005 from its target: that fixture is knife-edge
  and its verdict may flip on a library upgrade.

## Pending / Next
- ~~A4 — Drive folder ID~~ CLOSED as out of scope by operator direction, see D8.
  Cross-repo integration deferred; ClayPipe's output dir is the contract surface.
- Step 2 — scoring module. DONE, green. D9 and D10 CLOSED by Decisions 1 and 2.
- Step 3 — retry + firewalls. IN PROGRESS, cleared to build.
- D15 open (TF flags real motion) and D16 recorded — neither blocks step 3.
- D12 open: how should styles with no character (e.g. `logo`) score ID?
- Step 3 — retry + cost firewalls, incl. A3 cumulative PROJECT-level spend cap
  (the per-run `--max-cost-usd` alone does not stop ten aborted runs costing 10×).
- Step 4 — human gate HTML pages; Step 5 — FalBackend; Step 6 — CLI polish + README.
- A2 — Flux Kontext vs SDXL+ControlNet stays deferred until after the first real
  canary. Do not pre-optimize.
- Captions (Whisper → SRT) are not built yet; `assemble` already burns in an SRT
  if one is present at `subs.srt`, so step-6 wiring is a drop-in.
- RUNBOOK.md / CONFIG.md (Rule 33) not written yet — due with step 6.

## Log (append-only, newest first)

### 2026-09-09 — Decisions 1 and 2 applied; D9/D10 closed
- `weights.yaml`: `require_component_targets: true`, `tf_min` 0.80 -> 0.95, with
  the gate order documented in the file so it is unambiguous from config alone.
- `score.classify()` returns `(Verdict, VerdictReason)`; `FrameScore` carries
  `reason` and `missed_targets`.
- Re-swept all 11 fixtures. `structure_destroyed` PASS -> BORDERLINE as intended;
  `low_light`, `gaussian_blurred` and `high_motion` also moved (D16).
- New finding D15: at tf_min=0.95, real motion (0.932) is flagged alongside
  flicker (0.916). Recorded, not absorbed.
- 80 tests green.

### 2026-09-09 — Step 2 shipped: scoring module + 11 fixture cases
- Built `pipeline/score.py`: SSIM, LPIPS-on-Canny-edges, open_clip identity,
  Farneback optical-flow temporal fidelity, composite F, PASS/BORDERLINE/FAIL,
  and a per-component `targets_met` diagnostic. Models sit behind
  `PerceptualDistance` / `IdentityEmbedder` protocols.
- `weights.yaml` gained `canny`, `temporal` and `models` sections; all validated
  on startup by new pydantic models. Still nothing numeric hardcoded in score.py.
- `tests/fixtures/generate.py` writes 11 deterministic cases from fixed
  arithmetic (no RNG in the images, no third-party assets): identical,
  recolored_only, edge_shifted, gaussian_blurred, face_swapped, low_light,
  neumorphic, structure_destroyed, wrong_scene, plus static/high_motion/flicker
  pairs for temporal.
- Two fixtures had to be strengthened mid-step, and both taught something:
  the first flicker fixture used small random blobs and scored TF 0.978 — ABOVE
  real motion (0.932), an inverted ranking — because TF is a whole-frame mean.
  Rebuilt as realistic whole-surface shimmer, which ranks correctly (0.916) and
  exposed D10. Separately, no original fixture reached the FAIL band at all, so
  `structure_destroyed` and `wrong_scene` were added; only the latter FAILs.
- Caught D11 (QuickGELU) from a warning rather than ignoring it.
- Suite: 76 passed warm, 43 passed / 33 skipped cold. Step 3 NOT started.

### 2026-09-09 — Resume: two deviations resolved before any new code
- D1 (PIL header overlay) APPROVED by operator as a deliberate, permanent design
  choice. Recorded as ship-state, not a pending repair.
- D8: `output.drive_folder_id` DELETED from styles.yaml, config.py, run.py and
  tests/test_config.py per operator direction. Suite 20 -> 19 tests, still green.
  The deleted test asserted only that a null-only field was null, so it
  constrained no behaviour — the operator is right that it tested nothing.
- Logged the SPEC.md-vs-build-brief authority conflict under D8 so the next agent
  does not re-add the field by following SPEC.md A4.

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
