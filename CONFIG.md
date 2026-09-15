# CONFIG — ClayPipe

Every tunable value, where it lives, and **what it means**. Rule 25: nothing
here may be hardcoded in Python; Rule 33: this file must match the code, and
drift is a defect.

Two files, validated on startup by `claypipe/config.py`. A bad value is a crash
naming the field, never a silent default. Both models are `extra: forbid`, so a
leftover key from an older version is also a crash naming the key.

- **`styles.yaml`** — what the output looks like.
- **`weights.yaml`** — how output is judged, and what it is allowed to cost.

Secrets live in neither. `FAL_KEY` comes from the environment (`.env`, local
only, gitignored) and the pipeline fails fast at startup if a paid backend is
requested without it.

---

## styles.yaml

### `render` — the canvas

| Key | Shipped | Meaning |
|---|---|---|
| `width` | 1080 | Canvas width. Must be even (`yuv420p` subsamples chroma 2x2). |
| `height` | 1920 | Canvas height. Must be even. |
| `caption_gap_fraction` | 0.037 | The caption band between the two panels, as a fraction of `height`. 72px at 1920. |
| `crf` | 18 | x264 quality. Lower is bigger and better. |
| `pix_fmt` | `yuv420p` | Output pixel format. The evenness rules above exist because of it. |
| `default_fps` | 12 | Extraction fps when `intake --fps` is omitted. |
| `header_font_size` | 54 | Starting size for the header bar. Shrinks to fit. |
| `caption_font_size` | 40 | Starting size for caption text. Shrinks to fit the gap band. |
| `font_candidates` | 4 paths | First existing path wins. Startup fails loudly if none exist — never a silent fallback to a default font. |

**There is deliberately no `panel_height`, `header_height` or `divider_height`.**
Panel and margin geometry is DERIVED from each source clip's own aspect ratio at
render time (`claypipe/pipeline/layout.py`). A 16:9 source and a 1:1 source need
completely different panels and both are correct; the old fixed 882px panel
cropped real picture away from every source that was not exactly 1080x882.

`caption_gap_fraction` is the one vertical figure still under operator control,
because T12 draws captions INTO that band rather than burning them over the
picture. Measured on the reference clips: 0.0371 (New Girl, 16:9) and 0.0312
(Reacher, 2.01:1). One value cannot fit both exactly; 0.037 favours the
denser-captioned case. Per-title tuning is legitimate.

#### Derived geometry, for reference (do not paste these back into the config)

Two branches, and which applies is derived from a margin floor, not configured:

```
WIDTH-FIT   (aspect >= 1.436)   panels span the canvas, height follows aspect
HEIGHT-FIT  (aspect <  1.436)   panels fit the vertical space, width follows
                                aspect, centred with background pillarboxing
```

| Source | Mode | Panel | x | gap | top | bottom |
|---|---|---|---|---|---|---|
| 16:9 | width | 1080x608 | 0 | 72 | 316 | 316 |
| 2.014:1 | width | 1080x536 | 0 | 72 | 388 | 388 |
| 4:3 | height | 1002x752 | 39 | 72 | 172 | 172 |
| 1:1 | height | 752x752 | 164 | 72 | 172 | 172 |

The cutoff `1.436` comes from `MIN_MARGIN_FRACTION = 0.09` (172px of 1920) in
`layout.py`, via `W / ((H - gap - 2*min_margin) / 2)`. The 9% is constrained
from both sides: above `MIN_HEADER_HEIGHT` (96px, legibility) and below the
margins the reference format actually uses (16.6% and 20.6%), so it binds only
on aspects the reference clips never covered.

### `output`

| Key | Shipped | Meaning |
|---|---|---|
| `runs_dir` | `runs` | Where finished runs are written. ClayPipe's contract surface ends here. |

### `styles.<name>` — one profile per look

| Key | Meaning |
|---|---|
| `prompt` | The style prompt sent to the backend. |
| `strength` | img2img strength, 0-1. Higher = more restyle, less structure. |
| `background_color` | Canvas background, `#RRGGBB`. Themed per title (§1.4). |
| `header_text_color` | Header and caption ink. The background's contrast partner. |

Shipped profiles: **`clay`** (the target — see memory.md D40), `lego` (the
reference clips' look, kept because the format works for it), `logo`.

---

## weights.yaml

### `weights` — the composite F

```
F = w.ssim*SSIM + w.lpips_edges*(1 - LPIPS_edges) + w.id*ID + w.tf*TF
```

The FORMULA is fixed in code. The weights are config and must sum to 1.0.

| Key | Shipped |
|---|---|
| `ssim` | 0.40 |
| `lpips_edges` | 0.25 |
| `id` | 0.20 |
| `tf` | 0.15 |

### `targets` — per-component gates (Track A / canonical)

| Key | Shipped | Meaning |
|---|---|---|
| `ssim_min` | 0.72 | Structural similarity to the source. |
| `lpips_edges_max` | 0.35 | Perceptual distance on Canny edges. Lower is better. |
| `id_min` | 0.85 | CLIP cosine against the locked identity references. |
| `tf_min` | 0.95 | Temporal fidelity. Raised from 0.80 by Decision 2 (D14) — at 0.80 it was unbreachable. |

These are GATING, not diagnostic — see `thresholds.require_component_targets`.

### `thresholds` — the verdict bands

| Key | Shipped | Meaning |
|---|---|---|
| `pass` | 0.75 | `F >= pass` AND all component targets met -> auto-accept. |
| `borderline` | 0.65 | `borderline <= F < pass` -> retry once at lower strength. |
| `require_component_targets` | true | Decision 1 (D13). Without it, only SSIM could single-handedly fail a frame. |

**Gate order matters and is the point** (see the long comment in the file):
composite FAIL is settled first, then component targets, then composite F. The
only route to FAIL is the composite; a missed component target reaches
BORDERLINE and no further.

### `modes` — per-track target vectors (T13)

The scoring gate is mode-dependent. `surface` (Track A, per-frame img2img)
preserves geometry, so structure preservation is what is graded. `resynth`
(Track C, video-native) legitimately MOVES geometry, so the surface targets
would reject good output — but temporal consistency is Track C's entire claim,
so `tf_min` goes **up**, not down.

| Key | surface | resynth |
|---|---|---|
| `targets.ssim_min` | 0.72 | 0.45 |
| `targets.lpips_edges_max` | 0.35 | 0.55 |
| `targets.id_min` | 0.85 | 0.80 |
| `targets.tf_min` | 0.95 | **0.97** |
| `aggregation` | `block_p95` | `block_p95` |
| `max_retries_per_unit` | 2 | 1 |
| `total_retry_budget_fraction` | 0.15 | 0.05 |
| `calibrated` | true | **false** |
| `calibration_note` | where the numbers came from | why they are placeholders |

`calibration_note` is required whenever `calibrated` is false, and validated as
non-empty: a future session must be able to tell a placeholder from a
measurement without reading git history.

`modes.surface` restates the canonical `targets` and `firewalls` numbers
exactly, and a validator refuses any mismatch. The restatement exists so Track
C can differ, **not** so the two can drift.

The retry policy differs for a structural reason: a failed FRAME reseeds one
frame, a failed CLIP CHUNK reseeds 81-240 frames. One Track C retry can cost
more than ten Track A retries, so sharing a cap would let a single chunk
failure eat the run.

> **`resynth.calibrated: false` is a live firewall — the mode is NOT CALIBRATED
> and cannot spend.** Its numbers have the right shape and no measurement
> behind them. `batch` FIREWALL 0b refuses a paid run in an uncalibrated mode. T16 replaces the numbers and flips the flag
> **in the same commit that records the decision**. Do not relax the gate to
> get a run through.

### `canny`, `temporal`, `models` — how the metrics are computed

| Key | Shipped | Meaning |
|---|---|---|
| `canny.blur_kernel` | 5 | Pre-blur before edge detection. Must be odd. |
| `canny.low_threshold` | 100 | Canny hysteresis low. Must be below high. |
| `canny.high_threshold` | 200 | Canny hysteresis high. |
| `temporal.aggregation` | `block_p95` | How the motion-compensated residual becomes one number. **This choice is load-bearing** — see D34. |
| `temporal.block_size` | 4 | Tile size for `block_p95`. |
| `temporal.pyr_scale` | 0.5 | Farneback pyramid scale between levels. |
| `temporal.levels` | 3 | Farneback pyramid levels. |
| `temporal.winsize` | 15 | Farneback averaging window. Larger = more robust to noise, less able to resolve small motion. |
| `temporal.iterations` | 3 | Farneback iterations per pyramid level. |
| `temporal.poly_n` | 5 | Farneback pixel neighbourhood for the polynomial expansion. |
| `temporal.poly_sigma` | 1.2 | Gaussian sigma for that expansion. |

The six Farneback parameters are **shared** by temporal fidelity and by T18's
keyframe decision (`propagate.flow_between`), so the thing deciding a keyframe
and the thing grading temporal drift cannot disagree about what motion is.
Changing them changes both.
| `models.lpips_net` | `alex` | LPIPS backbone. |
| `models.identity_model` | `ViT-B-32-quickgelu` | open_clip arch. **Not** `ViT-B-32` — see D11. |
| `models.identity_pretrained` | `openai` | open_clip weights. |

`block_p95` is the median of per-block 95th percentiles. It is what separates
flicker from motion: shimmer raises the high percentile in MOST blocks so the
median rises with it, while real motion raises it enormously in the FEW blocks
holding occlusion edges and the median steps over them. A whole-frame p95
reports the occlusion edges and nothing else.

### `firewalls` — the five cost firewalls

| Key | Shipped | Meaning |
|---|---|---|
| `max_retries_per_frame` | 2 | Per-frame retry cap. |
| `total_retry_budget_fraction` | 0.15 | Whole-run retry budget, as a fraction of frame count. |
| `borderline_strength_delta` | -0.10 | Strength nudge on a `targets_missed` retry (D18). Must be negative. |
| `kill_switch.after_frames` | 50 | Check the running mean after this many frames. |
| `kill_switch.min_mean_f` | 0.70 | Halt below this. Never discover a bad run at frame 700. |
| `cost.max_cost_usd_run` | `null` | Per-run cap. `null` = uncapped. `--max-cost-usd` overrides. |
| `cost.max_cost_usd_project` | `null` | Project-wide cap (SPEC A3): the per-run cap alone does not stop ten aborted runs at $19 each. |
| `fal.model` | `fal-ai/flux-pro/kontext` | The fal endpoint. |

### `firewalls.cost.pricing` — unit-aware prices (T15)

A flat per-call number mis-prices most candidate backends. Each entry declares
**what it charges for**.

All non-dummy rates were read off fal's own model pages on **2026-09-15** and
each entry in `weights.yaml` names the endpoint it came from, so a future
session can re-check one line instead of re-researching the table.

| Backend | fal endpoint | Unit | Rate | Round up to MP |
|---|---|---|---|---|
| `dummy` | — | image | 0.0 | — |
| `dummy_clip` | — | video_second | 0.0 | — |
| `fal` | `fal-ai/flux-pro/kontext` | image | 0.04 | n/a |
| `fal_kontext_dev` | `fal-ai/flux-kontext/dev` | megapixel | 0.025 | **yes, confirmed wording** |
| `fal_qwen_image_edit` | `fal-ai/qwen-image-edit` | megapixel | 0.030 | assumed (page silent) |
| `fal_flux_general` | `fal-ai/flux-general/image-to-image` | megapixel | 0.075 | **yes, confirmed** |
| `wan_vace_480p` | `fal-ai/wan-vace-14b` | video_second | 0.04 | — |
| `wan_vace_580p` | `fal-ai/wan-vace-14b` | video_second | 0.06 | — |
| `wan_vace_720p` | `fal-ai/wan-vace-14b` | video_second | 0.08 | — |
| `runway_aleph` | (deferred) | video_second | 0.18 | — |

`fal_flux_general` is the only fal endpoint accepting BOTH `controlnets` and
`loras` (by URL), so it is the sole candidate able to test
structure-conditioning together with a claymation LoRA. It is 3x Kontext
[dev]'s rate.

Wan VACE's `num_frames` is constrained to **81-241 inclusive** at 16fps native,
so the smallest purchasable request is 81/16 = **5.0625 video-seconds =
$0.2025**. `DummyClipBackend` mirrors those bounds deliberately.

**A billing unit fal uses that this model does NOT support:** some endpoints
(e.g. `fal-ai/fast-sdxl-controlnet-canny/image-to-image`) are priced **per
compute second**. That cannot be authorised before the call, because the
duration is unknown until after it — and the ledger authorises first by design.
Such a backend is therefore refused as unpriced, which is the correct outcome
rather than a gap to paper over. See memory.md D53.

`estimated_usd_per_call` remains the per-IMAGE shorthand every existing caller
uses; a backend appearing in both must agree, enforced by a validator.

**`round_up_to_mp` is fal's real billing rule and it inverts an optimisation.**
fal rounds up to the next whole megapixel, so 640x640 (0.41MP) pays the 1MP
rate and 1.05MP pays for 2MP. On a hosted per-MP backend the cheap move is to
render LARGE; on self-hosted GPU, cost is roughly linear in pixels, so the
cheap move is to render SMALL. Render geometry therefore cannot be a fixed
aesthetic constant.

Three refusals rather than conveniences:
- `per_call()` on a non-per-image backend **raises** instead of answering.
- Pricing a unit backend without its dimension **raises** — defaulting it to 1
  would under-report spend and leave the caps not binding.
- An entirely unpriced backend is **refused**. `dummy_clip` is listed for
  exactly this reason: without it the whole Track C path is untestable.

### `review` — the human gates

| Key | Shipped | Meaning |
|---|---|---|
| `outlier_f_threshold` | 0.75 | Frames below this go on the flag page. |
| `audit_sample_fraction` | 0.02 | Random sample of PASSING frames, so a clean run is still spot-checked. |
| `audit_sample_seed` | 20260909 | Fixed, so the audit sample is reproducible. |
| `max_outlier_cards` | 200 | Page cap. Exceeding it writes an incident with the true count. |
| `canary_timeout_s` | 600 | `batch --wait-for-canary` poll timeout. |
| `canary_poll_s` | 1.0 | Poll interval. |

---

## Values that are NOT config, and why

| Thing | Where | Why not config |
|---|---|---|
| The F formula | `score.py` | Changing it invalidates every historical score. The weights are config; the shape is not. |
| Layout geometry | `layout.py` | Derived from each source's aspect ratio. A configured panel height is wrong for some source by construction. |
| `MIN_MARGIN_FRACTION`, `MIN_HEADER_HEIGHT`, `MIN_PANEL_HEIGHT` | `layout.py` | Legibility and fit floors, not taste. Changing them changes which fit branch a clip takes. |
| Shot detector threshold (27.0) | `shots.py` | A property of PySceneDetect's detector, not a tuning knob on the fidelity score. |
| `SEED_BASE` (1000) | `shots.py` | Seed = 1000 + shot index. Reproducibility, not preference. |
| Keyframe residual / chain limits | `propagate.py` | Measured defaults with a stated provenance; overridable per run via `--keyframe-residual-max` / `--keyframe-max-chain`. |
| Caption padding and line spacing | `captions.py` | Derived from the band height. Two different line-height definitions is what caused the T12 overflow bug (D52). |
| Burn-in detector thresholds | `burnin.py` | Calibrated against four real clips; see T9b. |
