# RUNBOOK — ClayPipe

Operating the pipeline: how to run it, pause it, resume it, re-run it, read its
logs, and recover when it fails. Rule 33 — this file must match the code.

ClayPipe is a **local CLI**. Nothing is deployed, and no scheduler runs it. It
spends money only when you pass `--live` with a paid backend, and five
independent firewalls sit in front of that.

---

## 0. Before anything

```bash
cd ~/dev/claypipe
source .venv/bin/activate          # or use .venv/bin/python directly
claypipe version                   # config + ffmpeg validation, no side effects
```

`version` loads and validates both config files and resolves ffmpeg. If it
fails, nothing downstream will work — fix that first.

**Requirements.** ffmpeg + ffprobe on PATH (or `CLAYPIPE_FFMPEG` /
`CLAYPIPE_FFPROBE` pointing at binaries). Python 3.11+. Optional extras, staged
per build step (D3):

```bash
pip install -e '.[scoring]'    # SSIM / LPIPS / CLIP — needed by any paid run
pip install -e '.[shots]'      # PySceneDetect — needed by batch
pip install -e '.[captions]'   # faster-whisper — only for `claypipe captions`
pip install -e '.[fal]'        # the fal client
pip install -e '.[dev]'        # pytest
```

**Secrets.** `FAL_KEY` in `.env` (local only, gitignored, mode 600). Never
committed, never in a config file. A paid run without it fails at startup
naming the variable.

---

## 1. The happy path

Six stages. Each writes only inside the run directory, which is what makes a
crashed run resumable.

```bash
# STAGE 0 — register the clip. Probes it, locks the style, detects burned-in text.
RUN=$(claypipe intake input.mp4 --style clay --title "Show (S01E01)")

# STAGE 1 — restyle ONLY the 3 canary frames, then review and decide.
claypipe canary restyle "$RUN"          # add --live for a paid backend
claypipe canary render "$RUN"
claypipe canary pack "$RUN"             # opens the page in a browser
claypipe canary submit "$RUN" --url '<paste the address bar here>'

# STAGE 2 — captions (optional; local, free)
claypipe captions "$RUN" --model base
$EDITOR "$RUN/cues.json"                # fix transcription, add [music] / *sfx*

# STAGE 3 — the full batch.
claypipe batch "$RUN" --propagate       # add --live for a paid backend

# STAGE 4 — assemble the 9:16 comparison and re-mux the original audio.
claypipe assemble "$RUN"

# Any time:
claypipe status "$RUN"
claypipe export "$RUN"                  # JSON snapshot, no local paths
```

`$RUN` is a directory path; a bare run id also works.

### Track C (video-native) differs in one place

```bash
RUN=$(claypipe intake input.mp4 --style clay --mode resynth)
claypipe canary restyle "$RUN" --clip 5.1    # NOT 3s — see §6
```

A resynth run **refuses** a three-frame canary, and `batch` refuses a resynth
run whose canary was the wrong kind. Three stills cannot canary a video model.

---

## 2. Pause / resume / re-run

**Pause:** Ctrl-C. Every stage is resume-safe because an existing output file is
never regenerated, so a killed run never re-spends for work already done.

**Resume:** re-run the same command. It picks up where it stopped.

```bash
claypipe batch "$RUN"        # restyles only the frames that are missing
```

The log reports `resumed=<n>` so you can see what was reused rather than paid
for again. The canary's frames are resumed by the full batch — they are paid
for once.

**Re-run a stage from scratch:** delete its output, then re-run.

| To redo | Delete |
|---|---|
| frame extraction | `$RUN/frames/source/` (including `.extract_complete`) |
| the restyle | `$RUN/frames/restyled/` |
| shot detection | `$RUN/shots.json` |
| the keyframe plan | `$RUN/keyframes.json` |
| captions | `$RUN/cues.json` and `$RUN/frames/captions/` |
| the canary decision | `$RUN/canary_verdict.json` |
| assembly | `$RUN/final_comparison.mp4` |

Extraction resumability is decided by a **sentinel**, not a frame count: a
directory with frames in it only proves ffmpeg started. The sentinel records the
frame count AND the source hash, so a changed input re-extracts instead of
reusing frames belonging to a different video.

**Re-run the whole clip:** `claypipe intake` again. Runs are immutable and
timestamped; nothing is overwritten.

---

## 3. Logs and artefacts

Everything is inside the run directory.

| Path | What it holds |
|---|---|
| `run.json` | The manifest: what this run is, decided at intake. |
| `logs/run.jsonl` | Structured JSON log, one object per event, with `runId`. |
| `frames/source/` | Extracted frames + `.extract_complete` sentinel. |
| `frames/restyled/` | Restyled (and propagated) frames. |
| `frames/captions/` | The caption band as a PNG sequence. |
| `refs/` | Locked identity references, copied from the approved canary. |
| `shots.json` | Shot boundaries, per-shot seeds, motion ranking. |
| `keyframes.json` | Which frames were paid for and which were warped. |
| `scores.jsonl` | Per-frame gate scores. One object per scored frame. |
| `drift.jsonl` | Source-referenced propagation drift (T18a). **Not** gate scores. |
| `spend_ledger.jsonl` | Append-only spend record, with unit and billed dimension. |
| `incidents/` | One file per firewall breach, with the full context. |
| `canary_review.html` | The canary gate page. Offline, self-contained. |
| `flag_review.html` | Flagged + audit frames, with the drift panel. |
| `qc_card.json` | The run's summary card. |
| `final_comparison.mp4` | The deliverable. |

Project-level, in `runs_dir`: `spend_ledger.jsonl` (project roll-up) and
`history.jsonl` (one QC card per run, for cross-clip tuning).

**Reading the log:**

```bash
jq -r '[.timestamp,.level,.event]|@tsv' "$RUN/logs/run.jsonl"     # timeline
jq 'select(.level=="WARN" or .level=="ERROR")' "$RUN/logs/run.jsonl"
jq 'select(.event=="spend.authorized")' "$RUN/logs/run.jsonl"     # every charge
jq -s 'map(select(.event=="authorized").estimated_usd)|add' \
   "$RUN/spend_ledger.jsonl"                                      # total
```

---

## 4. The firewalls, and what each refusal means

Six gates sit in front of spend. All of them are refusals, and **none has an
override flag** — that is deliberate (D17).

| # | Gate | Refusal looks like | What to do |
|---|---|---|---|
| 0 | Paid backend needs `--live` AND `FAL_KEY` | "requires --live" / "FAL_KEY is empty" | Both, deliberately. `--live` cannot arrive by accident. |
| 0b | Mode must be calibrated | "mode 'resynth' is NOT CALIBRATED" | Run T16, set the numbers from measurement, flip `calibrated`. Do not relax the gate. |
| 1 | Approved canary verdict | "canary gate: canary_verdict.json not found" | `canary restyle` → `render` → `submit`. |
| 1b | Canary must be the right KIND | "needs a CLIP canary" | `canary restyle --clip 5.1` on a resynth run. |
| 2 | Identity references must exist | "needs at least one reference image" | Approve a canary; it locks them automatically. |
| 3 | Spend caps | "run spend cap reached" | Raise `--max-cost-usd`, or accept the halt. Checked BEFORE the call. |

Plus, during a run: the **per-frame retry cap**, the **whole-run retry budget**
(15%), and the **kill switch** (halts if mean F is below 0.70 after 50 frames).
Each writes an incident file.

### Warnings that are not refusals

| Warning | Meaning |
|---|---|
| `intake.burned_in_text` | The source already has burned-in captions or a watermark. It will be restyled into the top panel and duplicated by the caption track. Acknowledge with `--allow-burned-captions`. |
| `batch.references.photoreal_risk` | Identity references came from `intake --ref`, not from an approved canary. Expect near-universal `id_min` misses (F1). Approve a canary instead. **Do not lower `id_min`.** |
| `assemble.layout.reprobed` | The run predates T9 and records no source dimensions, so they were re-probed. |
| `canary.restyle.below_min_chunk` | The requested clip canary is shorter than the backend's minimum chunk. It will bill the minimum anyway. Ask for more. |
| `shots.single_shot_forced` | `--single-shot` disabled per-shot seeding AND boundary-aware temporal scoring. |

---

## 5. Failure recovery

**`FAILED: ... has no audio stream`** — intake refuses silent clips; the audio
re-mux guarantee has nothing to guarantee. Add a track or use a different clip.

**`LayoutError: a 1.000:1 source cannot be laid out`** — only on a canvas too
short to stack two usable panels. Check `render.height` and
`caption_gap_fraction`.

**`ShotDetectionError: shot detection needs PySceneDetect`** —
`pip install -e '.[shots]'`. Do **not** work around it with `--single-shot`
unless the clip genuinely has no cuts: the fallback silently restores per-clip
seeding and false temporal flags at every cut.

**`AUDIO WAS NOT COPIED BIT-FOR-BIT`** — the sync guarantee is void. This is a
hard failure and must not be worked around. Re-run `assemble`; if it persists,
the extraction is suspect — delete `$RUN/audio.aac` and re-run `batch`.

**`reassembly frame-count mismatch` / `frame-count mismatch before assembly`** —
the restyle is incomplete. Re-run `batch`; it will fill the gaps. Assembly
refuses to guess which frames are missing.

**`ChunkLengthError`** — a clip backend returned a different number of frames
than it was given. A short chunk shortens the clip and desyncs the audio in a
file that plays, so the run halts. Retry; if it repeats, the endpoint is
misbehaving.

**`RunHalted: kill switch`** — mean F below `min_mean_f` after
`after_frames`. Read the incident file, look at the flag page, and fix the
prompt before re-running. Do not raise the threshold to get past it.

**Scoring wants to download model weights mid-run** — it must never. Warm the
cache deliberately before a paid run:

```bash
HF_HUB_OFFLINE=0 python -c "from claypipe.pipeline.score import models_are_cached; print(models_are_cached())"
```

---

## 6. Spending money — the checklist

Read this before any `--live` run.

1. `git status` is clean and pushed. Rule P2/P3.
2. `claypipe version` passes.
3. The backend is **priced** in `firewalls.cost.pricing`, with the right unit.
   An unpriced backend is refused; a wrongly-united one mis-charges the ledger.
4. The mode is **calibrated**, or you are running T16 to calibrate it.
5. A **run cap** is set: `--max-cost-usd <n>`. The default is uncapped.
6. Model weights are cached (above), so scoring cannot stall mid-run.
7. You have run the same clip end-to-end on `--backend dummy` first. It is free
   and it catches every wiring problem.
8. Start with the **canary**, never `batch`. Three frames, or one short clip.

### Known cost facts

- A 60s clip at 12fps is **720 frames**. Per-frame pricing multiplies by 720.
- `--propagate` cuts paid frames 5-12x (measured: 7.5x on a 16:9 reference
  slice, 4.97x on the busier square clip). Every frame still exists on disk, so
  the frame-count invariant and audio guarantee are untouched.
- A clip backend has a **minimum chunk**. Wan VACE's floor is 81 frames at
  16fps native = **5.06 video-seconds = $0.20**. A 3-second canary is 36 frames
  at 12fps and is **not purchasable** — it bills the minimum or pads the range,
  and padding breaks the duration invariant. Ask for at least 5.1s.

---

## 7. Reading a finished run

```bash
claypipe status "$RUN"        # stage, mode, pricing unit, identity, shots, burn-in
jq . "$RUN/qc_card.json"
open "$RUN/flag_review.html"
open "$RUN/final_comparison.mp4"
```

**`qc_card.json` fields worth knowing:**

- `cost_usd` comes from the **spend ledger**, never from a backend branch (D29).
- `scores` is `null` on an unscored run, not zero — an unmeasured run must never
  read as a clean one.
- `mode` says which target vector the scores were graded against. A card
  without it cannot be compared to another card.
- `propagation_drift` is the T18a source-referenced check. It is reported
  BESIDE `scores`, never inside them, and is **not gated**: temporal fidelity
  cannot measure propagation drift, because a propagated frame is a warp along
  the optical flow and TF grades by warping along the optical flow — the metric
  and the generation method are the same operation. `null` means the run did
  not propagate, which is not the same as drift measured at zero.

---

## 8. Session discipline (Rule 0)

1. Read `memory.md` **first**, before any code or commands.
2. Commit and push after every checkpoint, not just at session end.
3. Verify, do not assume: `git ls-files` for secrets (not `.gitignore`),
   `git status` + `git log origin/main..HEAD` for sync.
4. Session end: memory updated → committed → pushed → pending work logged.
5. No silent exits. State what was done and what is still pending.
