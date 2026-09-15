# ClayPipe — Master Plan

**Self-contained.** Supersedes `CLAYPIPE_REVIEW_AND_BUILD_BRIEF.md`,
`CLAYPIPE_BACKEND_COST_RESEARCH.md`, and `CLAYPIPE_FINAL_REVIEW_AND_BUILDOUT.md`.
Keep those as working notes; build from this one.

Scope decision (operator, 2026-09-14): **Track A + Track C.** Track B is out of
scope for this phase and is recorded in §9 so it isn't re-litigated.

**Target style is CLAYMATION** (operator, 2026-09-14). The two reference clips
are LEGO-minifig restyles. They are the reference for *format, layout, cadence
and method* — not for the look. Everywhere this document says "the reference
does X", the aesthetic target is the claymation equivalent: plasticine puppets,
visible fingerprints and tool marks, matte clay with subsurface warmth,
handmade miniature sets. The minifig look is never the goal.

Inputs: `qwazi12/claypipe` (README, memory.md D1–D39, SPEC), `qwazi12/scrapper`,
`qwazi12/socialpilot_Ai`, frame-level analysis of two @trevorcarlee reference
clips, backend pricing researched 2026-09-14.

**Measurement provenance.** Every number in §1 was re-measured from the two
source files on 2026-09-14 by this session, independently of the analysis that
produced the original plan. Three corrections came out of that re-measurement
and are marked **[CORRECTED]** below. Everything else reproduced.

---

## 0. Blockers — clear before writing code

**B1 — Rotate `FAL_KEY`.** D36 records a key pasted into a session transcript.
Never used, never committed, but Rule 8 says a disclosed secret is rotated, not
avoided. A replacement key was installed locally 2026-09-14 (`.env`, gitignored,
mode 600); B1 is satisfied only if that key is *new*, not the disclosed one.

**B2 — Revoke the GCP service-account key in `socialpilot_Ai`.** memory.md
records `socialpilot-ui/config/googleAuth.json` as still tracked in a **public**
repo (commit `59c9a14`), holding a real private key with Editor access to the
sheet. Harmless while nothing touches that repo. Not harmless once T24 wires
ClayPipe to it.

```
cd socialpilot_Ai
git ls-files | grep -i googleAuth          # verify still tracked
# 1. REVOKE the key in GCP IAM  <- first; the only step that actually helps,
#    because the key is already in public git history
# 2. issue a new key, store as env var / secret manager entry
# 3. git rm --cached socialpilot-ui/config/googleAuth.json
# 4. add to .gitignore, commit
# 5. optionally scrub history (git-filter-repo) — cosmetic after step 1
```

**B3 — Railway account.** CLI is authenticated as `shoppykid1@gmail.com`, not
`kyeboah@kymediamgmt.com`. Decide which account owns the ClayPipe service before
`railway link`; scrapper's backend already lives somewhere and should probably
be the same account.

---

## 1. The target, measured

Two @trevorcarlee TikTok clips: New Girl S01E19 (87.28 s, 2094 frames) and
Reacher S01E02 (62.29 s, 1495 frames). Both 576x1024, h264, **24.000 fps**,
AAC 44.1 kHz stereo. Verified by ffprobe 2026-09-14.

Method: ffprobe streams; per-row temporal variance over 120 frames for band
geometry; per-panel inter-frame mean absolute difference for cadence;
96x54-downscaled panel-delta thresholding for cuts; ebur128 for loudness.

### 1.1 Layout

Measured band edges (row indices in the 576x1024 source):

```
Clip A (New Girl)                    Clip B (Reacher)
y    0- 169  background  h=170       y    0- 210  background  h=211
y  170- 494  PANEL restyled h=325    y  211- 496  PANEL restyled h=286
             aspect 1.772                          aspect 2.014
y  495- 532  caption gap  h= 38      y  497- 528  caption gap  h= 32
y  533- 856  PANEL original h=324    y  529- 809  PANEL original h=281
             aspect 1.778                          aspect 2.050
y  857-1023  background  h=167       y  810-1023  background  h=214
```

Three rules fall out:

- **Panel aspect follows the SOURCE's native aspect.** 1.78 for New Girl, 2.01
  for Reacher. Panels are always full canvas width; height is derived.
  "Source-aspect not yet configurable" is not an open item — it *is* the layout
  engine.
- **The panel pair is vertically centred; the header lives in the top-margin
  remainder.** Measured top-vs-bottom margin delta: +3 px (A), -3 px (B).
  Symmetric to within 3 px in both directions, which is rounding, not a rule.
- **Captions sit in the gap band BETWEEN panels**, ~3.1-3.7% of canvas height —
  a layout element, not a subtitle burn over video.

**[CORRECTED] The plan's normalised 1080x1920 table did not close
arithmetically.** It gave 16:9 as top 321 + panel 608 + gap 71 + panel 608 +
bottom 313 = **1921**, one pixel over the canvas, and its asymmetric margins
contradicted its own `block_centered` rule. The margins are *derived from the
rule*, never tabulated. The layout engine computes:

```
panel_h   = even(canvas_width / source_aspect)
gap       = even(gap_fraction * canvas_height)
remainder = canvas_height - 2*panel_h - gap
top       = even(remainder / 2)          # header lives here
bottom    = remainder - top              # equals top when remainder % 4 == 0
```

| Element | Rule | 16:9 source | 2.01:1 source |
|---|---|---|---|
| Top margin (header) | `even(remainder/2)` | 316 px | 388 px |
| Panel (each) | `even(1080 / source_aspect)` | 608 px | 536 px |
| Caption gap | `even(0.037 * 1920)` | 72 px | 72 px |
| Bottom margin | `remainder - top` | 316 px | 388 px |

Every dimension is even, because `yuv420p` requires it, and the four terms sum
to exactly 1920. Against the scaled measurement (panel 609/536, gap 71/60,
margins 319/313 and 396/401) the derived panels land within 1 px and the
margins within 8 px — the margin gap being entirely clip B's caption band,
which measures 0.0312 of canvas height against the configured 0.037. Gap fraction is
config, not code, and is per-title tunable.

### 1.2 Cadence — the 12 fps decision, validated

Per-frame mean absolute difference, top and bottom panels measured separately,
480 frames from t=10 s:

| | Restyled panel held | Effective fps | Original panel held | Effective fps |
|---|---|---|---|---|
| Clip A | 53.9% | **11.1** | 18.8% | 19.5 |
| Clip B | 53.2% | **11.2** | 38.4% | 14.8 |

Gap distribution between successive *changed* restyled frames:

```
Clip A:  1f: 54   2f: 150   3f: 1   4f: 7   5f: 1   6f: 3   8f: 2   (+2 long holds)
Clip B:  1f: 75   2f: 139   3f: 1   4f: 2   6f: 1                   (+5 long holds)
```

**The restyled panel updates every 2 frames in a 24 fps container — exactly
12 fps, animated on twos.** The 2-frame gap is the mode in both clips (150/219
and 139/222). 12 fps was recorded as a cost decision that happened to match the
aesthetic; it matches the reference to the frame. Do not change it.

Note the two panels have *different* cadences: stylisation is stepped, the
original runs near full rate. The `trim`-bounded filtergraph (D6) already
handles panels of differing length.

### 1.3 Shot structure — this sets the keyframe budget

Cut detection on the original panel (96x54 downscale, mean-delta > 22):

| | Frames | Cuts | Shots | Avg shot | Median | Shortest |
|---|---|---|---|---|---|---|
| Clip A | 2094 | 41 | 42 | **2.08 s** | 1.62 s | 0.62 s |
| Clip B | 1495 | 14 | 15 | **4.15 s** | 0.46 s | 0.21 s |

**[CORRECTED] Clip B's average shot length is 4.15 s, not 2.35 s.** 14 cuts in
62.29 s is 15 shots at 4.15 s mean. The corrected figure makes the bimodality
*stronger*, not weaker: a 0.46 s median against a 4.15 s mean means clip B is
mostly long dialogue holds punctuated by very short action bursts. That is
precisely the distribution adaptive keyframing exploits and a fixed 1-in-6 ratio
cannot.

Clip A is the denser case at **28.9 shots per 60 s**; clip B runs 14.4. Budget
against clip A. ~24 frames per shot at 12 fps. With shot detection plus flow
propagation a clip needs **~30-60 paid generations instead of 720** — 12-24x,
not the 6x the SPEC's EbSynth note assumes.

### 1.4 Styling

- **Background:** themed per show. A: flat `#015DB1`. B: textured gold
  ~`#BB860C` with visible grain.
- **Header:** `{title} (S{season:02d}E{episode:02d})`, centred, bold grotesque
  sans, background's contrast partner (white on blue, black on gold). Casing
  follows the show's own logo treatment.
- **Captions:** same font, centred in the gap band, sentence case with terminal
  punctuation, phrase-level (no word-by-word karaoke). Full closed-caption style
  including non-dialogue: `[dramatic music]`, `*crunch*`, `*smack*`,
  `*grunting in pain*`, `*clapping*`.
- **Audio:** clip A measures -24.2 LUFS, 9.3 LU range — untouched broadcast
  audio, no normalisation pass. Consistent with the re-mux guarantee. (B's
  -14.1 is likely TikTok's upload normalisation, not the creator's mix.)
- **Outro:** clip B ends with a ~10 s branded sequence. Clip A's tail is
  TikTok's download end-card, not content.

### 1.5 What the reference actually is — and what we are actually making

At full resolution the reference's restyled panel shows a cylindrical head with
printed features, C-shaped claw hands, a moulded one-piece hairpiece, a
trapezoid torso with line-art fabric folds. The geometry has been **replaced**,
not reskinned. Environments stay photoreal-ish while only characters are
rebuilt — character replacement over matched plates. That is rigged 3D
animation, and it is where the format's moat is.

**Our target is the same method, in claymation.** Not minifigs: plasticine
puppets with visible fingerprints, tool marks, seams where limbs meet the body,
matte surfaces with slight subsurface warmth, and the microscopic frame-to-frame
wobble real stop-motion has. Claymation is a *better* fit for this pipeline than
LEGO for two reasons worth recording:

1. **Clay tolerates geometry drift; plastic does not.** A minifig is a
   manufactured object — a head that is 5% too tall or a claw hand with four
   fingers reads instantly as wrong. A clay puppet is handmade by definition, so
   the same generative variance reads as craft. That moves a chunk of Track B's
   difficulty into Track A/C's reach.
2. **On-twos stepping is native to clay.** Stop-motion *is* clay animation's
   real production constraint, so the 12 fps cadence in §1.2 stops being a cost
   compromise that happens to look stylish and becomes the medium's actual
   signature.

Frame-by-frame img2img still cannot do true character *replacement*, and
ControlNet makes it less able to, because ControlNet exists to preserve exactly
the geometry that would have to change. That is why Track B is a different
engine. But the claymation target narrows the gap Tracks A and C have to cross,
which is why they are the ones that ship.

---

## 2. The two tracks

### Track A — Surface restyle (per-frame, what ClayPipe is)

Geometry preserved, surface and palette transformed: claymation texture,
cel-shaded, watercolour, flat-vector, anime. Genuine img2img strengths. The
existing SSIM >= 0.72 and LPIPS-edges <= 0.35 targets are **correct in this
mode**, because structure preservation is the goal here.

### Track C — Video-native resynthesis (per-clip)

Wan VACE, Runway Aleph, DomoAI. These resynthesise rather than reskin, so they
shift geometry further than img2img, and they are temporally consistent by
construction. They will not reliably produce correct puppet anatomy — but they
get materially closer to a "rebuilt in clay" look than Track A, and at 480p
they are cheaper per clip than per-frame at 12 fps.

### What running both costs architecturally

Track C is not a new backend behind the existing protocol — it violates three
assumptions.

**A1 — The scoring gate is mode-dependent.** SSIM 0.72 is right for Track A and
wrong for Track C: a resynthesised frame legitimately moves geometry, so the
surface-mode targets would reject good output. `weights.yaml` needs per-mode
target vectors, not one set:

```yaml
modes:
  surface:                    # Track A
    ssim_min: 0.72
    lpips_edges_max: 0.35
    id_min: 0.85
    tf_min: 0.95
    aggregation: block_p95
  resynth:                    # Track C
    ssim_min: 0.45            # calibrate at T16, do not guess
    lpips_edges_max: 0.55
    id_min: 0.80
    tf_min: 0.97              # HIGHER — temporal consistency is C's whole claim
    aggregation: block_p95
```

The `resynth` numbers have the correct *shape*, not the right values. T16
produces the first real measurement; set them from that and record it as a
decision. Shipping guessed thresholds on a paid backend is the failure mode F4
warns about.

**A2 — The frame-count invariant becomes a duration invariant.** The guarantee
is `extracted == restyled == reassembled == output`, and it is what makes the
audio re-mux provably safe. VACE works in chunks of 81-240 frames at 16 fps. So:

- Keep the audio MD5 packet hash as the **hard gate, unchanged**. It is the
  thing that actually matters and it still works.
- Replace frame-count equality with: output duration equals source duration to
  within one frame at 12 fps (+/-83 ms), AND the audio hash matches. If the hash
  passes, the invariant held in the form that counts.
- Resample VACE's 16 fps output back to 12 fps on the restyled panel so the
  on-twos cadence from §1.2 survives. **Decimation, not interpolation** —
  interpolation would smooth out the exact stop-motion stepping that §1.5 says
  is claymation's signature.

Record as a decision. A future session reading "frames as images, never video"
in SPEC §3 will otherwise revert it.

**A3 — The canary gate needs a clip mode.** Three still frames cannot canary a
video model; temporal behaviour is the only reason to use one. Add
`canary --clip <seconds>` that renders a short trim instead of three frames.
Same gate, same verdict file, same approve/reject/adjust, same "no override
flag" rule (D17).

**[CORRECTED] A 3-second VACE canary is not purchasable, and costs $0.20 not
$0.12.** VACE's minimum chunk is 81 frames at 16fps native = 5.06 video-seconds
= $0.20 at $0.04/video-second. A 3-second canary at the pipeline's 12fps is 36
frames, which VACE cannot honour at all — it would bill its minimum, or pad the
range, and padding changes the frame count and breaks the duration invariant.
So the clip-canary floor is **5.06s / $0.20**, and asking for less buys a
smaller canary at the same price. The firewall economics still don't change
(a three-frame Kontext-pro canary is $0.12, the same order), but the number and
the minimum duration were both wrong. T14 warns with this arithmetic rather
than letting it surface on an invoice.

The trim is anchored on the MOST-MOTION shot, not the head of the clip: a video
model's failure mode is temporal — flicker, smearing, identity wandering — and
the opening seconds are often a static establishing shot where none of it
shows. Canarying the calm part of a clip is how a temporal model passes a gate
it should fail.

**A4 — Two protocols, one seam.** Keep `RestyleBackend` (per-frame) and add
`ClipRestyleBackend` (per-range) alongside it. Don't generalise one into the
other — the retry policy differs fundamentally. A failed frame reseeds one
frame; a failed clip chunk reseeds 81-240 frames at once, so the per-frame retry
cap and the 15% retry budget need per-mode values too. A single retry on a
Track C chunk can consume more budget than ten Track A retries.

---

## 3. Backends and cost

Unit: **one 60-second clip at 12 fps = 720 frames.** All list prices as
researched 2026-09-14 — verify in console before committing a batch; several are
promotional.

### Track A candidates (per-frame)

| Backend | List price | 720 frames |
|---|---|---|
| fal Kontext **[max]** | $0.08/image | $57.60 |
| **fal Kontext [pro] — current default** | $0.04/image | **$28.80** |
| Replicate Kontext [pro] | ~$0.055/image | $39.60 |
| fal Kontext [dev] | $0.025/megapixel | ~$18.00 |
| fal Qwen Image Edit 2511 | $0.03/megapixel | ~$21.60 |
| **SiliconFlow Kontext [dev]** | **$0.015/image** | **$10.80** |
| Segmind / fal SDXL | ~$0.006/image | $4.32 |
| **Replicate SDXL** | ~$0.0046/image | $3.31 |
| DeepInfra SDXL | ~$0.004/image | $2.88 |
| **Replicate SD 1.5** | ~$0.0023/image | $1.66 |
| DeepInfra SD 1.5 | ~$0.002/image | $1.44 |

### Track C candidates (per video-second)

| Backend | Price | 60 s clip |
|---|---|---|
| **fal Wan VACE 14B @ 480p** | $0.04/video-sec | **$2.40** |
| fal Wan VACE 14B @ 580p | $0.06/video-sec | $3.60 |
| fal Wan VACE 14B @ 720p | $0.08/video-sec | $4.80 |
| Runway Gen-4 Aleph (WaveSpeed) | $0.18/video-sec | $10.80 |
| Wan VACE **self-hosted** (Modal/RunPod) | GPU time | ~$0.50-1.50 |

### Self-hosted serverless — where production should land

| Setup | Rate | ~s/frame | 720 frames |
|---|---|---|---|
| **Modal T4 + SD1.5 + ControlNet + clay LoRA** | $0.59/hr | ~1.5 s | **~$0.18** |
| Modal A10G + SDXL + ControlNet | $1.10/hr | ~3 s | ~$0.66 |
| Modal + Wan VACE | $1.10/hr | — | ~$0.50-1.50 |
| RunPod serverless flex (16 GB) | $0.58/hr | ~1.5 s | ~$0.17 |
| RunPod Pod RTX A5000 24 GB | $0.27/hr | ~1.5 s | ~$0.08 |
| Local GPU (8 GB+) | electricity | — | ~$0.00 |

**Modal's free tier is the headline: $30/month in credits that renew
automatically, no card required** — roughly 27 A10G-hours or ~50 T4-hours. At
~1.5 s per SD1.5 frame that is **~160 sixty-second clips a month at zero
marginal cost.** For daily posting that is the entire production budget,
permanently. Caveats: regional multipliers (1.25x US/EU) and no paid
non-preemptible GPU tier — fine for batch frame work, since a preempted frame is
just a retry and a retry controller already exists.

A **clay LoRA** is the single highest-leverage item on this table. It is what
makes a $0.0023 SD1.5 call competitive with a $0.04 Kontext call for this
specific look, because the style no longer has to be carried by the prompt.
Train or source one before T19.

RunPod is slightly cheaper at the Pod tier but has no free tier and needs a card
to evaluate. Modal wins on the credits.

### Two traps

**Licensing.** FLUX.1 Kontext [dev] is **free for non-commercial use only;
commercial use of the weights is $999/month.** So "self-host Kontext dev on
Modal for pennies" is not available to a monetised pipeline. Hosted resellers
(fal, SiliconFlow) bundle commercial rights, which is why the hosted dev rate is
the right way to reach that model. Self-hosting is only the cheap path for
permissive weights: SD1.5, SDXL, Wan/VACE. Check the licence before
containerising anything.

**Billing granularity.** fal bills per megapixel **rounded up to the next whole
megapixel** — so at 640x640 (0.41 MP) you pay the 1 MP rate. On hosted per-MP
backends, render large; on self-hosted GPU, cost is roughly linear in pixels, so
render small. The same `styles.yaml` value has opposite optimal settings
depending on backend, which means render geometry cannot stay a fixed aesthetic
constant. Record as a decision.

### Combined picture

| Configuration | 60 s clip | Clips per $10 |
|---|---|---|
| **Today:** Kontext pro, every frame | $28.80 | 0.35 |
| Kontext [dev] hosted, every frame | $10.80 | 0.9 |
| Replicate SDXL, every frame | $3.31 | 3 |
| **fal Wan VACE 480p (Track C)** | **$2.40** | 4 |
| Replicate SD1.5, every frame | $1.66 | 6 |
| Replicate SDXL + adaptive keyframes | $0.55 | 18 |
| **Modal T4 + SD1.5 + CN + clay LoRA (Track A)** | **$0.18** | 55 |
| Modal + SD1.5 + adaptive keyframes | $0.03 | 330 |
| **Any Modal config, inside $30 free credits** | **$0.00** | ~160/mo |

Roughly **1,000x** between the current default and the realistic production
config. That is an architecture problem, not a tuning problem.

### DomoAI — benchmark, not backend

$9.99-69.99/month; the Pro tier has **unlimited Relax Mode**, which beats
per-frame pricing at any volume. It still cannot be a backend: no API means no
`RestyleBackend`, no spend ledger, no Fidelity Score, no canary gate, no retry
policy — every firewall goes inert.

But it is the best $10 of the week as a **reference generator**. Run three real
clips through its claymation style. Then the achievable-without-3D ceiling is
known, and there is output to score the pipeline against instead of a target
only ever seen on someone else's TikTok.

---

## 4. Seven repo findings

**F1 — The identity reference points at the wrong images. This breaks the first
paid run.** `--ref` locks Stage-0 references and ID targets >= 0.85 via open_clip
ViT-B-32-quickgelu. The calibration note ("restyled == source scores ID 1.000")
is a null test — it measures an image against itself. A real clay restyle scored
against a *photoreal* source reference will land ~0.65-0.82, because CLIP
embeddings move hard under total style transfer. Then: nearly every frame misses
`id_min` -> BORDERLINE (D13) -> retry at strength -0.10 (D18) -> the 15% retry
budget halts the run at ~107 frames -> the backend gets blamed. **Fix:
references are the *approved canary output*, not the source.** ID then asks "is
this the same clay character the operator approved" — the question the metric
exists for. Also closes D12 (the `logo` style with no character): the reference
is whatever the canary approved. Do not lower `id_min` instead; that trades a
calibration bug for a blind gate.

**F2 — `shots.py` is the keystone, not a leftover.** D30 files it as a
documentation-honesty note. Three problems collapse into it: the keyframe cost
reduction (§1.3), false TF flags at hard cuts (a cut looks exactly like
catastrophic temporal failure to a flow-warped diff — the D15/D34 fight), and
"per-shot fixed seed" which is currently per-clip. PySceneDetect
`ContentDetector` is already an optional extra (D3).

**F3 — The cost table prices per frame; you pay per clip.** §3 above.

**F4 — Thresholds calibrated on synthetic fixtures are about to gate real
money.** All 11 fixtures are deterministic arithmetic — recolors, blurs, edge
shifts. None is a generative restyle. `gaussian_blurred` already sits 0.005 from
its target (D16). Right now the first real measurement happens *during a paid
batch*. Move it into the canary: print the full component vector per frame,
refuse to batch until the operator has seen observed-vs-target. Reporting
change, not a threshold change.

**F5 — `export` cannot carry what the dashboard needs.** D33 says "no frames" —
right for a status snapshot, fatal for approving a canary you cannot see. Extend
deliberately: `publish` uploads canary frames as downscaled JPEGs (long edge
<= 768, q80, ~60-100 KB each) through the same `assert_no_local_paths()` scrub,
with a hard per-snapshot byte cap. Metadata in JSON, pixels in blob storage
keyed by run ID. Record as a new decision so a future session doesn't "restore"
D33.

**F6 — Remote approval inverts the spend-authority model unless it is inverted
back.** D17 is the sharpest decision in the repo: the canary gate lives at the
command boundary with deliberately no override flag, and the ledger sits one
layer below so money control cannot be stepped around. D32 then puts an approve
button on the public internet. If the Railway API can cause a local batch to
start, that is the override flag D17 refused to build — reachable by anyone with
a token. **Keep the polarity: the dashboard is advisory, the laptop is
authoritative.** The remote *records* a verdict and triggers nothing.
`claypipe publish --poll` fetches it; local code decides. `--live` and the spend
caps stay local. Every verdict signed with a per-run secret and carrying the
canary frames' content hash, so a verdict for frames you did not publish is
rejected (which also makes it idempotent). The invariant to write a test for:
*a fully compromised Railway service can waste your time and leak run metadata;
it cannot spend a dollar.*

**F7 — `socialpilot_Ai` is the wrong integration surface but the right
scheduler.** 80+ one-off scripts at repo root, a README describing an unrelated
scaffold, and the B2 credential. Its real interface is a Drive folder and a
Sheets row. Write to those directly through a thin adapter; import nothing.
Preserves D8's spirit — ClayPipe's output is the contract surface — while giving
the chain.

---

## 5. Web architecture (D32)

```
  +- LOCAL (authoritative) -----------------+
  |  claypipe CLI                           |
  |  ffmpeg . torch . GPU . runs/ on disk   |
  |  spend ledger . firewalls . --live      |
  +--------+-------------------^------------+
           | publish           | poll verdict
           | (snapshot+JPEGs)  | (signed, advisory)
  +--------v-------------------+------------+
  |  RAILWAY — FastAPI + Postgres + Volume  |
  |  stores snapshots, serves them,         |
  |  records verdicts. Starts nothing.      |
  +--------^--------------------------------+
           | REST, bearer token
  +--------+--------------------------------+
  |  VERCEL — Next.js dashboard             |
  |  runs list . canary approve . flags     |
  |  cost + score charts . video preview    |
  +-----------------------------------------+
```

Deliberately the same shape as `scrapper` (FastAPI->Railway, Next.js->Vercel,
shared token in a settings panel), already live at `scrapper.nodepilot.dev`.
**Reuse that deployment pattern** — Railway config, CORS setup,
token-in-a-panel UX, `.railwayignore`. It has been debugged once already.

### Auth — the UNDECIDED item in memory.md, settled

- **Shared bearer token** in `Authorization`, same as scrapper. Not a login
  system; one user, and OAuth is weeks of work for one person.
- Token in Railway env + Vercel env, never in the repo, rotatable by changing one
  variable.
- **Every endpoint authenticated, including reads.** Run metadata includes spend.
  No public read tier.
- **Verdicts additionally signed** with a per-run secret generated at `intake`,
  shared at `publish` (F6). Token compromise leaks data; it must not forge an
  approval.
- Rate-limit the verdict endpoint. Log every verdict with timestamp and IP to an
  append-only table — same discipline as `history.jsonl`.

### Data model (Postgres)

```sql
runs      (run_id PK, project, style, mode, stage, created_at, updated_at,
           backend, cost_usd, frames_total, frames_done, verdict_state,
           snapshot JSONB)
frames    (run_id FK, idx, kind, blob_key, f_score,
           ssim, lpips_edges, id_score, tf, verdict, reason)
verdicts  (id PK, run_id FK, decision, prompt_override, frame_hash,
           signature, created_at, source_ip)
incidents (run_id FK, ts, kind, detail)
```

`snapshot JSONB` holds the `claypipe export` document verbatim, so the API never
redeploys when the snapshot schema evolves. Columns are a projection for
querying; the JSONB is the truth. `runs.mode` distinguishes Track A from Track C
so the dashboard renders the right target vector. Railway managed Postgres
~$5/mo; the JPEG/preview volume is pennies.

### API surface

```
POST /runs/{id}/snapshot     upsert, body = export document
POST /runs/{id}/frames       multipart, canary/flag JPEGs
GET  /runs                   list + status
GET  /runs/{id}              detail + scores
GET  /runs/{id}/verdict      local CLI polls this
POST /runs/{id}/verdict      dashboard writes; signed; idempotent
GET  /runs/{id}/preview.mp4  assembled output, if uploaded
```

Note what is absent: no `POST /runs/{id}/start`, no `/batch`, no `/retry`. The
API cannot cause work. Put a comment in the router explaining why the obvious
endpoint is not there.

### Dashboard

Runs list (stage, F distribution sparkline, spend, gate state) . **canary
review** — frames beside their source, full component vectors against the mode's
targets, Approve/Reject/Adjust with prompt override; this is the screen that
justifies the build, since the gate currently requires being at the machine .
flag review (`verdi/flag_page.py` grid, remote and paginated) . cost against run
and project caps, with running cost-per-published-clip . history
(`history.jsonl` as a sortable table — cross-clip tuning is explicitly the
operator's job, so give the operator the view).

Keep the local `file://` review pages. They are the offline fallback and cost
nothing to keep. D21 stays true; the dashboard is additive.

---

## 6. Integration contracts

**scrapper -> ClayPipe.** scrapper already downloads from ~1800 sites and writes
`logs/manifest.jsonl`. *Option A (recommended):* a "Send to ClayPipe" button
POSTs `{url, title, style, mode}`; local ClayPipe polls
`GET /jobs?status=pending`, downloads via yt-dlp, runs `intake`. *Option B
(today, zero code):* scrapper writes to `downloads/`, ClayPipe's `intake` takes
a path. **Poll, don't push** — never let Railway push work to the laptop (F6).

**ClayPipe -> publishing.** One command, no socialpilot imports:

```
claypipe publish-final <run-id>
  -> refuses unless assemble succeeded and the QC card is clean
  -> uploads final_comparison.mp4 to a configured Drive folder
  -> appends a Sheets row: {title, drive_url, status: "Review",
                            source_url, cost_usd, mean_F, run_id, mode}
  -> records the row id back into the run manifest
```

socialpilot's cron picks up `Review` rows exactly as today; nothing in that repo
changes. This re-opens D8/A4 in a controlled way (a Drive folder ID in config) —
record it as a superseding decision so a future session doesn't re-litigate from
SPEC.md A4. **Gated on B2.**

**Full chain:** scrapper (live) -> ClayPipe local -> dashboard (approve from
anywhere) -> Drive + Sheets -> socialpilot cron -> posted. Three of five boxes
already exist.

---

## 7. TARGET_DNA

```json
{
  "format": "side_by_side_restyle_9x16",
  "reference_look": "lego_minifig",
  "target_look": "claymation",
  "target_look_note": "reference clips are LEGO; the format, layout, cadence and method are the reference, the look is not",
  "canvas": { "width": 1080, "height": 1920, "fps_container": 24 },
  "restyle": {
    "fps_effective": 12,
    "cadence": "on_twos",
    "measurement": "measured 2026-09-14: 2-frame holds dominant, 150/219 (A), 139/222 (B); effective 11.1 / 11.2 fps"
  },
  "layout": {
    "rule": "panel_height = even(canvas_width / source_aspect)",
    "panel_width": 1080,
    "panel_order": ["restyled", "original"],
    "caption_gap_fraction": 0.037,
    "vertical_alignment": "block_centered",
    "header_placement": "top_margin_remainder",
    "margins_are_derived_not_tabulated": true,
    "derived_examples": {
      "16x9":   { "panel_h": 608, "gap": 72, "top_margin": 316, "bottom_margin": 316 },
      "2.01x1": { "panel_h": 536, "gap": 72, "top_margin": 388, "bottom_margin": 388 }
    },
    "measured_source_bands": {
      "clip_a_576x1024": { "top": 170, "panel": 325, "gap": 38, "panel2": 324, "bottom": 167 },
      "clip_b_576x1024": { "top": 211, "panel": 286, "gap": 32, "panel2": 281, "bottom": 214 }
    }
  },
  "theme": {
    "background_mode": "per_title",
    "examples": [
      { "title": "New Girl", "bg": "#015DB1", "fg": "#FFFFFF", "texture": false },
      { "title": "Reacher",  "bg": "#BB860C", "fg": "#000000", "texture": true }
    ],
    "font_style": "bold_grotesque_sans",
    "header_format": "{title} (S{season:02d}E{episode:02d})",
    "header_casing": "match_source_logo"
  },
  "captions": {
    "placement": "gap_band",
    "burn_method": "layout_element_not_subtitle_filter",
    "granularity": "phrase",
    "style": "sentence_case_with_terminal_punctuation",
    "include_nondialogue": true,
    "nondialogue_forms": ["[music cue]", "*sfx*", "*action*"]
  },
  "shots": {
    "avg_length_seconds_dense": 2.08,
    "avg_length_seconds_sparse": 4.15,
    "median_length_seconds": [1.62, 0.46],
    "shortest_observed_seconds": 0.21,
    "shots_per_60s_budget": 29,
    "measurement": "measured 2026-09-14: 41 cuts / 42 shots in 87.28s (A), 14 cuts / 15 shots in 62.29s (B)"
  },
  "audio": {
    "policy": "remux_untouched",
    "normalization": "none",
    "measured_lufs": [-24.2, -14.1]
  },
  "outro": { "present": true, "duration_seconds": 10 }
}
```

---

## 8. Build order

One commit per task with its own acceptance test, matching the T1-T8 convention.
**Do not start Phase 3 until Phase 2 has produced a video worth publishing.**

### Phase 1 — Match the target

| # | Task | Acceptance |
|---|---|---|
| **T9** | **Layout engine rewrite.** Panel height from measured source aspect; block-centred; caption gap band; header into top-margin remainder via the PIL overlay path (D1). Retire fixed geometry. | **DONE.** 16:9 -> 316/608/72/608/316. 2.014:1 -> 388/536/72/536/388. Four terms sum to 1920, all even, asserted on rendered pixels. |
| **T10** | **Reference-from-canary (F1).** `canary submit` writes approved restyled frames to `refs/`; `batch` locks those. Closes D12. | Clay frames score ID >= 0.85 against canary refs. Source-frame refs warn at startup, not silently accept. |
| **T11** | **`shots.py` (F2).** PySceneDetect boundaries; per-shot seeds; canary 3rd frame = real most-motion shot (closes D30); TF skips boundary frames. | 3-cut synthetic clip -> 4 shots, 4 seeds. Against clip A the detector finds 41+/-4 cuts. |
| **T12** | **Captions as layout, not burn-in.** Whisper -> phrase cues -> rendered into the gap band by the PIL compositor. Non-dialogue cues preserved. | Audio MD5 unchanged after captioning. Caption never overlaps either panel. |

### Phase 2 — Two tracks, measured, cheap

| # | Task | Acceptance |
|---|---|---|
| **T13** | **Mode split (A1/A4).** `ClipRestyleBackend` protocol alongside `RestyleBackend`; `modes:` block in `weights.yaml`; per-mode retry caps and budgets; `runs.mode` persisted. | **DONE.** resynth relaxes ssim/lpips/id and TIGHTENS tf_min; retry cap 1 vs 2 and budget 5% vs 15%. `calibrated: false` makes a paid resynth run a refusal (F4). Mode in `status` and the QC card. |
| **T14** | **Clip canary (A3).** `canary restyle --clip <seconds>` renders a trim, anchored on the most-motion shot. Same gate, same verdict file, no override flag. | **DONE.** A resynth run refuses a 3-frame canary at render AND at batch; the kind lives on the manifest so it cannot be forged through the submission URL. Warns below the backend's min chunk with the real arithmetic. |
| **T15** | **Ledger price model.** `weights.yaml` gains `{backend: {unit: image\|megapixel\|video_second, rate, round_up_to_mp}}`. | **DONE.** 60s VACE 480p prices at exactly $2.40; the MP round-up trap is modelled (0.41MP bills as 1MP). `per_call()` REFUSES a non-per-image backend rather than answering. Unpriced backend still refused. Ledger records the unit and billed dimension. |
| **T16** | **The bake-off, ~$0.95 total.** Track A 3-frame canary: Kontext pro ($0.12), SiliconFlow Kontext dev ($0.05), SDXL+CN ($0.02), SD1.5+CN+clay LoRA ($0.01), Qwen Edit ($0.09). Track C 3-second canary: Wan VACE 480p ($0.12), Aleph ($0.54). Print full component vectors. | A real clay-restyled frame and clip exist. Ledger reconciles to the cent. **Both mode target vectors set from measurement and recorded as decisions** (closes F4). |
| **T17** | **Duration invariant (A2).** Replace frame-count equality with duration-within-one-frame plus the unchanged audio MD5 gate. Decimate 16 fps -> 12 fps on the restyled panel. | A VACE output passes the audio hash. Restyled panel measures 12 fps effective on the §1.2 test. Decision recorded against SPEC §3. |
| **T18** | **Adaptive keyframe propagation (Track A).** Reuse the TF optical flow: restyle shot-first frames, warp forward, new keyframe when residual exceeds threshold. | **DONE, one criterion met and one refuted.** <= 60 paid frames IS reachable: 59 of 720 on a real 60s slice (12.2x) with an unbounded chain; the shipped default is 12, giving 96 paid (7.5x), conservative for the reason in §8a. **TF >= 0.99 is NOT met and is the wrong criterion** — see §8a. |
| **T19** | **Production backend.** Modal + SD1.5 + ControlNet + clay LoRA (Track A) and/or Modal + Wan VACE (Track C), both inside the free credits. | 12 s clip end-to-end under $0.05. F within 0.03 of the T16 winner or better. |

### 8a. T18 — two measured findings that contradict T18's own premises

Measured on a real 60-second reference slice (720 frames, 28 shots), with
DummyBackend generating the keyframes.

**FINDING 1 — temporal fidelity cannot validate propagation.** T18's second
acceptance criterion is "propagated frames score TF >= 0.99". Measured:

| max_chain | paid | reduction | TF min | TF mean | TF p5 | % >= 0.99 |
|---|---|---|---|---|---|---|
| 6 | 142 | 5.07x | 0.8833 | 0.9905 | 0.9587 | 75.4% |
| 12 | 96 | 7.50x | 0.8784 | 0.9921 | 0.9676 | 79.0% |
| 24 | 74 | 9.73x | 0.8951 | 0.9932 | 0.9755 | 83.0% |
| unbounded | 59 | 12.20x | 0.9216 | 0.9947 | 0.9814 | 84.4% |

TF gets **better** as chains get **longer** — the exact opposite of the drift
intuition the chain limit was built on. The reason is structural: a warped
frame is by construction a smooth resampling of its predecessor, so it is
almost perfectly temporally consistent, while a KEYFRAME is a fresh generation
that does not match its predecessor. Every keyframe *insertion* is a temporal
discontinuity, so fewer keyframes means better TF.

So TF does not measure propagation drift; it measures how often a chain is
interrupted. The criterion is not met (84.4% at best, mean 0.9947, min 0.92)
and tightening it would not make it meaningful. What catches smear is fidelity
TO THE SOURCE — SSIM and LPIPS-edges — not frame-to-frame consistency.

**FINDING 2 — SSIM-vs-source is flat in chain depth on this footage.** A
40-warp chain loses ~0.05 SSIM against its own source (0.367 at the keyframe,
0.315 at depth 40-44), so the chain limit earns very little here.

**Why the shipped default is still conservative (12, not unbounded):** both
measurements used DummyBackend, whose output is flat posterised colour fields.
Resampling a flat field is nearly lossless, so the dummy *cannot* exhibit the
drift that repeated resampling would cause in real clay texture, fingerprints
and tool marks — its evidence that long chains are safe is weakest exactly
where it matters. 12 still buys 7.5x ($3.84/clip instead of $28.80 on Kontext
pro); `--keyframe-max-chain` raises it, and 12.2x / 59 paid frames is where the
"<= 60" target lands. **Re-measure at T16 against a real backend before
changing the default.**

A note on the absolute SSIM figures: 0.33-0.37 against a target of 0.72 is not
a propagation failure, it is the dummy backend scoring against real footage —
the same posterise scores above 0.72 on the synthetic fixtures. That is F4 and
D48 again.

### Phase 3 — Make it visible

| # | Task | Acceptance |
|---|---|---|
| **T20** | **`publish` + frames channel (F5).** Capped JPEGs through the existing path scrub. Decision superseding D33's "no frames". | Snapshot with frames still passes `assert_no_local_paths()`. Byte cap enforced. Idempotent. |
| **T21** | **Railway API + Postgres (§5).** Bearer auth on every route including reads. Signed verdicts. No start/trigger endpoint. | A test asserting: **no API route can cause a frame to be generated.** Assert the absence in the router. |
| **T22** | **Vercel dashboard (§5).** Runs list, canary approve (both modes), flag review, cost. Reuse scrapper's pattern. | Approve a canary from a phone; local `batch --wait-for-canary` proceeds. Reject; local batch refuses. |

### Phase 4 — Chain it

| # | Task | Acceptance |
|---|---|---|
| **T23** | **scrapper -> ClayPipe (§6).** Job queue; local polls. | Job appears, local poll picks it up, `intake` runs. |
| **T24** | **`publish-final` adapter (§6). Blocked on B2.** | Row appears with status `Review`; socialpilot's cron picks it up unchanged. |

### Stop doing

- Treating Kontext pro as the default — $28.80/clip is ~1,000x the realistic
  production config.
- Calibrating thresholds on synthetic fixtures. T16 is the first real datum.
- Pointing identity references at source frames.
- Planning toward LEGO minifigs through an image API. The target is claymation
  (§1.5), and minifig-specific prompt work is wasted effort.

---

## 9. Out of scope, recorded so it isn't re-litigated

**Track B — Claymation character replacement (3D).** The reference format's
actual method (§1.5), retargeted to clay: a rigged clay-puppet library,
monocular pose estimation on the source performance, retarget to rig, camera
solve, lighting match, headless render. Months, different skill set, probably a
different repo. The Fidelity Score would need full redefinition — SSIM against
the source becomes meaningless; the right metrics are pose error against the
estimated skeleton and silhouette IoU against the source subject.

Everything built in Phases 1-4 transfers to Track B unchanged. The only thing
that changes is what sits behind the backend protocol — exactly the seam the
architecture was designed around. Revisit after Track A/C output has been
published and measured against real audience response.
