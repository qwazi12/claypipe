# MEMORY — ClayPipe

## Current State
- **ARCHITECTURE CHANGED 2026-09-16: whole-frame video-to-video (Wan VACE on
  fal), replacing per-frame img2img.** V1-V7 landed. See D59-D64.
- **V7 RAN AND RETURNED A CLEAR NEGATIVE. $0.6075 spent.** Wan VACE 14B at 480p
  produces a COLOUR GRADE, not claymation, at both available control signals.
  SSIM 0.905 — it preserves structure better than the retired img2img gate ever
  demanded. **Do not proceed to V8 on this configuration.** See D64 for the one
  untested confound (our own prompt) and the escalation options.
- pytest **546 passed / 6 skipped / 0 failed**.
- **T16 IS NOW SPENDABLE, AND UNSPENT. Awaiting the operator.** Everything that
  blocked it is done and free: T9a, T9b, T18a, RUNBOOK.md, CONFIG.md. All fal
  prices verified against the live model pages. See D53 for the budget
  correction and the two blockers that are NOT code.
- **MASTER_PLAN.md is now the single build plan.** It supersedes the three
  working-note briefs. Scope: Track A (per-frame surface restyle) + Track C
  (video-native resynthesis). Track B (3D character replacement) is OUT OF
  SCOPE and recorded in MASTER_PLAN §9 so it is not re-litigated.
- **TARGET LOOK IS CLAYMATION (operator, 2026-09-14).** The two @trevorcarlee
  reference clips are LEGO-minifig restyles. They are the reference for FORMAT,
  LAYOUT, CADENCE and METHOD — never for the look. See D40.
- **PHASE 1 COMPLETE: T9, T10, T11, T12.**
- **PHASE 2 PARTIALLY COMPLETE: T13, T14, T15, T18 done.** T16 (the bake-off)
  and T19 (Modal) are BLOCKED on operator action; T17's code is local but needs
  a real Track C output to verify against.
- pytest **440 passed / 5 skipped / 0 failed**. The 3
  skips are real-footage measurements that skip unless
  CLAYPIPE_REFERENCE_CLIP_A/B point at the reference clips.
- **THE FULL PIPELINE RUNS END TO END ON A REAL 60-SECOND CLIP** with every
  Phase 1+2 feature on, verified 2026-09-14:
  `intake -> canary restyle (3 frames) -> approve (refs locked from the
  approved output) -> batch --propagate (95 paid + 3 resumed + 622 warped =
  720, 7.5x) -> 8 cues incl. 4 non-dialogue -> assemble`
  Output 1080x1920 h264 12fps, 720 frames, 60.02s. Audio MD5
  `696ef9ddd2d9b391db3b40fdb0846a79` identical across `audio.aac`, the ORIGINAL
  SOURCE and the output. Layout 316/608/72 derived from the 16:9 source.
  A frame was extracted and LOOKED AT — which is how D51 was found.
- Phases 3-4 (T20-T24) not started. T21/T22 need a Railway account decision
  (B3). T24 is blocked on B2.
- **FAL_KEY is installed locally** at `.env` (gitignored, mode 600, verified
  absent from `git ls-files`). B1 — whether that key is NEW rather than the one
  disclosed in D36 — is the operator's to confirm.

## Prior state (pre-2026-09-14)
- **Steps 1-4 of 6 are COMPLETE and green.** 131 tests pass warm;
  97 pass / 34 skip / 0 fail on a cold clone.
- Step 4 shipped `claypipe/verdi/` — two self-contained review pages and the
  verdict loaders. The pages are NOT yet consumed by `cli.batch`; Step 5 wires
  them in. `cli.batch` still reads `canary_verdict.json` via
  `retry.require_canary_approval`, which correctly BLOCKS on `approved:
  "adjust"` (verified) since the adjust branch is Step 5 work.
- All five cost firewalls are live and enforced: canary gate, per-frame retry
  cap, whole-run retry budget, kill switch, run + project spend caps.
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
- **D20 (2026-09-09) Learned-metric model weights are warmed deliberately, never
  downloaded during a run or a test.**
  *(Numbering correction, 2026-09-09: this entry was originally written as D13,
  colliding with Operator Decision 1 which was also numbered D13 later the same
  day. Renumbered to D20 — the later D13 keeps its number because `weights.yaml`
  and `tests/test_score.py` already cite it. Nothing was deleted; only this
  heading changed.)* `tests/conftest.py` sets
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
  just flicker. CLOSED 2026-09-12 by D34 (block_p95 aggregation).** Measured: real motion 0.932, severe
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
- **D17 (2026-09-09) The canary gate is enforced at the `batch` COMMAND
  boundary, not inside the pipeline API.** SPEC §4.1 says "batch mode REFUSES to
  run", so `cli.batch` calls `require_canary_approval()` before it even
  constructs a backend. There is deliberately NO override flag: an escape hatch
  would defeat the only gate between a bad prompt and a full batch of paid calls.
  Consequence to know: a library caller invoking `restyle_frames()` directly
  bypasses the gate. That is why the SPEND LEDGER lives one layer down, at the
  call site — the gate controls stage ordering, the ledger controls money, and
  the money control cannot be stepped around.
  Verified live, not just in tests: no verdict -> exit 1, 0 frames produced;
  `approved: false` -> exit 1; `approved: true` -> 60 frames.
- **D18 (2026-09-09) AMBIGUITY IN THE BRIEF, resolved toward the specific rule.**
  Decision 1's prose says a `targets_missed` frame is BORDERLINE and takes "the
  same retry-and-flag path as today: one retry with strength -0.10, then human
  queue". The Step-3 acceptance bullet instead says `targets_missed` "triggers
  the same retry path as `F < 0.65`" — which is the FAIL path, i.e. a NEW SEED.
  These are different actions. Implemented per the Decision 1 prose
  (strength -0.10, seed retained), because it is the more specific and more
  detailed statement, and because a BORDERLINE frame is a near miss that a
  strength nudge can plausibly rescue while a reseed throws away a nearly-good
  result. `test_decision1_targets_missed_triggers_a_retry` asserts the property
  both readings agree on — that the frame IS retried and consumes budget.
  **Flag for the operator: say the word if you want the reseed path instead.**
- **D19 (2026-09-09) The PROJECT-level spend cap is implemented, per SPEC
  Addendum A3.** The 2026-09-09 resume brief §7.4 restates only the per-run
  `--max-cost-usd`; it does not mention the project cap and does not revoke A3,
  which remained in this file's Pending list. Implemented as a shared
  `project_spend_ledger.jsonl` in the runs directory plus
  `firewalls.cost.max_cost_usd_project` (default null = uncapped), so it is
  inert unless deliberately configured. Rationale unchanged from A3: the per-run
  cap cannot see previous runs, so ten aborted runs at $19 each still cost $190.
  Surfaced rather than added silently — say the word if you want it dropped.
- **D21 (2026-09-09) Review pages submit by FORM-ENCODED QUERY STRING, not by
  POST to a handler.** The Step-4 brief specifies
  `<form action="..." method="POST">` with a `format_submit.py` handler. SPEC §5
  says, flatly, **"No web server."** A POST needs something listening; a page
  opened from `file://` has nothing, and browsers drop `file://` POSTs on the
  floor. Resolved by keeping a REAL html form with real `name=` attributes and
  `method="get"` targeted at the page itself: submitting puts the entire
  decision in the address bar, form-encoded, which the operator copies back to
  the CLI in one line. This satisfies every stated constraint at once — no
  server, no network, no JavaScript dependency, works in a text browser or with
  a screen reader — and it keeps the brief's own requirement that "the Python
  loader parses the form-encoded body, not JSON". JavaScript, when available,
  only adds a copy button and a JSON download; persistence never depends on it.
- **D22 (2026-09-09) `format_submit.py` was referenced but never specified.**
  The brief says the form handler is "`claypipe format_submit.py` (see below)"
  and there is no "below". Implemented the parsing half as
  `verdi.loaders.parse_form_submission()`, which is the contract Step 5 needs;
  no `format_submit.py` file was invented. The operator-facing submit COMMAND is
  deliberately not added — the brief assigns CLI wiring to Step 5.
- **D23 (2026-09-09) The caveat parser accepts both `- **D<n>` and `### D<n>`.**
  The brief's acceptance test says the page "parses memory's `### D[0-9]+`
  headings", but this file has always written decisions as `- **D<n> ...**`
  bullets, and reformatting the whole audit trail to satisfy a parser would be
  the tail wagging the dog. `verdi.find_decisions()` accepts either shape.
- **D24 (2026-09-09) Review thresholds live in `weights.yaml`, not in code.**
  The 200-card cap, the 0.75 outlier line, the 2% audit fraction, its seed and
  the canary poll timeout are all config (Rule 25). The brief quoted 200 and 2%
  as literals; they are defaults in `review:` now, not constants.
- **D25 (2026-09-09) `Scorer` and `RetryController` are NOT yet consumed by
  `cli.batch`.** Step 3 built them; Step 5 wires them in. Until then a batch run
  produces no `scores.jsonl`, and any review page for such a run would otherwise
  imply a clean sweep when nothing was actually measured. The canary page
  therefore renders an explicit Caveats block quoting this entry whenever the
  run's backend is `dummy` or its scores are absent, so an operator can never
  read an unscored page as a pass.
- **D26 (2026-09-09) Auto-checkpoint safety net: `.claude/hooks/checkpoint.sh`
  on the Stop hook.** Operator asked for passive, persistent saving to GitHub and
  memory.md. That is an automated behaviour, so it is a HOOK — an instruction to
  the model cannot fire on an event. The script logs to memory.md, commits and
  pushes whenever the tree is dirty at the end of a turn, and is silent when the
  tree is clean (the normal case, since step commits are deliberate).
  Safety properties, all tested: refuses to commit when a secret-shaped FILENAME
  is present or when staged CONTENT matches a credential pattern (verified — a
  planted fake `ghp_` token was refused and unstaged); never force-pushes; never
  rewrites history; exits 0 on every failure path so a session is never blocked;
  reports a PUSH FAILED message rather than pretending GitHub is in sync.
  Auto-commits are labelled `chore: auto-checkpoint` so they stay distinguishable
  from the deliberate P2 step commits — those remain the real checkpoints.
  **NOT YET ARMED.** Writing `.claude/settings.json` was blocked by the
  permission classifier, in both the Bash and Write paths. The script is
  committed and working; registering it as a Stop hook needs the operator. See
  the "Arming the auto-checkpoint hook" note in Pending.
- **D27 (2026-09-09) Scoring is SKIPPED for the `dummy` backend, permanently.**
  The dummy's entire visual difference from the source is a posterise plus
  `saturation = 1.0 + (seed % 7) * 0.05` (`restyle.py`) — a deterministic palette
  nudge with no generative content. Scoring it would manufacture numbers that
  look like quality measurements while measuring nothing, and would run LPIPS and
  CLIP inference over every frame of every offline test run to do it. The skip is
  logged (`batch.scoring.skipped`) and printed ("not scored"), so an ungraded run
  READS as ungraded — the opposite of silent. On any other backend the scorer is
  mandatory: a paid run that is not scored is a paid run nobody can defend.
- **D28 (2026-09-09) A paid run with no locked reference images is refused BEFORE
  the ledger authorises anything.** Identity carries weight 0.20 in a fixed
  formula, so with no reference there is no defined score (D12). Discovering that
  at frame 1 would mean money already spent, so the check runs at startup.
  `intake` gained `--ref` (repeatable) to lock Stage-0 references; it was skipped
  in Step 1 as speculative and is now genuinely required.
- **D29 (2026-09-09) QC-card cost is derived from the SPEND LEDGER, never from a
  backend branch.** The card previously hardcoded zeros when `backend == "dummy"`.
  Inferring cost from which backend was selected reports a plausible number
  instead of the real one, and can quietly disagree with the ledger the firewalls
  actually enforce against. Scores likewise come from `scores.jsonl`, and a run
  that was never scored reports `null` rather than zero — an unmeasured run must
  never read as a clean one.
- **D30 (2026-09-09) The canary's third frame is the LAST frame, not the
  most-motion shot — a STATED stand-in.** SPEC §4.1 asks for first / middle /
  most-motion, but shot detection (`shots.py`) has never been assigned a build
  step: the Build Order goes scoring -> firewalls -> gates -> Fal -> polish and
  never lands it. Rather than invent a motion heuristic mid-step, the third slot
  samples the far end of the clip and the PAGE SAYS SO ("last frame (stand-in
  for most-motion)"). An operator reading the canary can therefore tell that the
  hardest shot may not be represented — which is the risk a silent substitution
  would have hidden. **`shots.py` remains unassigned; it needs a build slot.**
- **D31 (2026-09-09) `canary_verdict.json` is written atomically.** The gate
  polls that path, so a half-written file could be caught mid-read. Written to
  `.json.tmp` then `Path.replace`d, which is atomic within a filesystem. Proven
  by intercepting the rename, not by inspection.
- **D32 (2026-09-09, OPERATOR DIRECTION) ClayPipe gets a remote dashboard.
  This CHANGES the scope rule.** Asked what should go to Railway and Vercel, the
  operator answered: *"i want to be able to see track and interact with this
  tool."* That reverses the standing "ClayPipe produces an mp4 and stops" rule
  (build-brief §14) and sits beside SPEC §5's "No web server" — which was written
  about the REVIEW PAGES needing no local server, and is not violated by a
  separately hosted dashboard, but is adjacent enough to name here.
  **The build brief and SPEC §14 are superseded on this point by the operator's
  2026-09-09 direction.** Recorded so a later session does not "correct" it back.
  Architecture, forced by the fact that ClayPipe runs LOCALLY (ffmpeg, torch,
  60+ frames a clip) and its state is a directory on the operator's disk that
  nothing hosted can see:
      ClayPipe (local)  --snapshot-->  Railway (API + store)  <--  Vercel (UI)
                        <--verdict---
  Interaction means approving or rejecting a canary from the dashboard, which is
  genuinely useful: the gate currently requires being at the machine.
- **D33 (2026-09-09) `claypipe export` is the dashboard contract, and it is
  path-scrubbed by construction.** A snapshot is built to LEAVE the machine, so
  it carries basenames only, metadata only, no frames and no secrets (Rule 6).
  `assert_no_local_paths()` refuses to publish a document containing `/Users/`,
  `/home/`, `/private/var/` or a Windows drive path, and incident notes have
  their absolute `run_dir` stripped. The snapshot is DERIVED: the run directory
  stays authoritative and a snapshot can always be rebuilt.
- **Accounts (verified 2026-09-09):** Vercel CLI authenticated as `qwazi12`;
  Railway CLI authenticated as **`shoppykid1@gmail.com`**, which is NOT the
  operator's work address (`kyeboah@kymediamgmt.com`) — flagged, not assumed
  wrong. No Railway project linked to this repo yet.
- **SocialPilot P0 carried, not adopted:** `socialpilot-ui/config/googleAuth.json`
  is STILL TRACKED in that repo (`git ls-files` confirms; committed in 59c9a14)
  and holds a real GCP service-account private key with Editor access to the
  sheet. Operator said "disregard for now and keep building" on 2026-09-09, so it
  stays open THERE. Recorded here only so it is not lost: deploying that repo
  anywhere copies a live key into another build environment.
- **D34 (2026-09-12) D15 CLOSED — temporal aggregation is `block_p95`, and the
  proposed `p95` was measured WORSE than the mean it was meant to replace.**
  Re-sweep on the three temporal fixtures, tf_min = 0.95:
  | fixture | mean | p95 | block_p95 |
  |---|---|---|---|
  | static_pair | 0.997 | 1.000 | **1.000** |
  | high_motion (legitimate motion) | 0.932 | 0.561 | **0.973** |
  | flicker_pair (severe shimmer) | 0.916 | 0.812 | **0.912** |
  * `mean` — correct ordering, but legitimate motion (0.932) cannot clear
    tf_min. That was D15.
  * `p95` — **INVERTS the ordering**: motion 0.561 scores WORSE than flicker
    0.812. A high percentile is precisely where real motion lives, because
    occlusion edges produce the largest residuals in the frame. The Step-6
    brief proposed p95 as the D15 fix; measurement says it would have made D15
    worse. Kept as an opt-in and pinned by a test so nobody re-adopts it.
  * `block_p95` — median of per-4x4-block 95th percentiles. **Passes all three
    assertions.** Shimmer raises the high percentile in MOST blocks so the
    median rises with it; motion raises it enormously in the FEW blocks holding
    occlusion edges and the median steps over them. That is the distinction a
    whole-frame statistic cannot make.
  Taken via the brief's own documented fallback ("if p95 fails any of the three
  assertions ... switch default to block_p95"). `mean` and `p95` remain
  selectable in `weights.yaml`; the chosen default and its full assertion list
  are locked by tests.
  **Consequence for the Definition of Done:** DoD item 5 asks that
  `temporal.aggregation` print exactly `p95`. It prints `block_p95` — the value
  the brief's fallback selects. Reported rather than forced.
- **D35 (2026-09-12) RESERVED, UNUSED.** The Step-6 brief reserved D35 for the
  branch where `p95` AND `block_p95` both fail the temporal assertions, forcing
  a revert to `mean`. That branch did not occur — `block_p95` passed all three
  on first measurement (D34). Recorded so the gap in numbering is explained
  rather than looking like a lost entry.
- **D36 (2026-09-12) T7 deferred — FAL_KEY absent; dry-run-only verification.**
  No live fal.ai call has been made from this repo, ever. The environment had
  no `FAL_KEY` at T7 time, so the brief's own skip condition applied and the
  commit chain goes T1-T6 then T8.
  **A key WAS pasted into the session transcript by the operator.** It was not
  used, not written to any file, and not committed. It must be treated as
  exposed and rotated at fal.ai before any live run (Rule 8: a disclosed secret
  is rotated, not just avoided). The operator stated they would set the key
  themselves, which is the right handling — ClayPipe reads `FAL_KEY` from the
  environment and never from an argument, because a command-line key lands in
  shell history.
  T7's two network-independent tests (`test_fal_real_api.py`) are deferred with
  it: they exercise `RealFalClient`, which T7 would have introduced and which
  does not exist. What DOES exist and is tested: the two-lock guard (T4), the
  stubbed backend, the price ceiling and the dry-run ledger path (T5).
  **Operator action to resume T7:** rotate the key, `export FAL_KEY=...`,
  `pip install -e '.[fal]'`, then re-run this task.
- **D37 (2026-09-12) Day-end invariants: pytest GREEN.** 190 passed, 0 failed,
  0 skipped warm. Cold clone (model caches emptied): **151 passed, 39 skipped,
  0 failed.** Every one of the 39 skips was checked with `-rs` and carries the
  learned-metric reason (LPIPS/CLIP weights not cached); there is no skip from
  any other cause.
  *Correction: the commit message for T8 states 157/33. That was written before
  the cold run finished and is wrong; the measured figures are 151/39. Left in
  the commit message rather than rewriting history, corrected here, which is
  what this file is for.*
- **D38 (2026-09-12) TODO/FIXME/XXX audit: none.** `grep -rn "TODO\|FIXME\|XXX"
  claypipe/ tests/` returns nothing. Recorded as required, not as an
  achievement — it was already clean.
- **D39 (2026-09-12) Four brief-vs-repo discrepancies, resolved toward the
  repo** (authority ordering puts this prompt lowest):
  1. `python -m claypipe` did not work — no `__main__.py`. DoD items 6 and 7
     invoke it, so it was added in T4.
  2. `batch` had no `--backend` flag; DoD 6/7 pass it. Added as an override of
     the value locked at intake, persisted to run.json.
  3. The brief's T6 transcript expects `runs/<id>/work/canary_review.html`. The
     page is written to the run ROOT, not `work/`. Transcript reflects reality.
  4. The brief's T6 expects `export` to write `export.json`/`index.json`
     implicitly. It prints to stdout unless `--out` is given; the transcript
     passes `--out` explicitly.

- **D40 (2026-09-14, OPERATOR DIRECTION) The target look is CLAYMATION; the
  reference clips are LEGO.** The two @trevorcarlee clips drive format, layout,
  cadence and method. They do NOT drive the aesthetic. Plasticine puppets with
  visible fingerprints and tool marks, seams where limbs meet the body, matte
  surfaces with slight subsurface warmth. Two consequences worth recording,
  because they change what is worth building:
  (a) **Clay tolerates geometry drift; plastic does not.** A minifig is a
      manufactured object — a head 5% too tall, or a claw hand with four
      fingers, reads instantly as wrong. A clay puppet is handmade by
      definition, so the same generative variance reads as craft. That moves a
      chunk of Track B's difficulty into Track A/C's reach.
  (b) **On-twos stepping is native to clay.** Stop-motion IS clay animation's
      real production constraint, so the 12fps cadence stops being a cost
      compromise that happens to look stylish and becomes the medium's
      signature.
  `styles.yaml` already ships a `lego` profile. It stays (it costs nothing and
  the format works for it), but `clay` is the target. Do not spend effort on
  minifig-specific prompt work.
- **D41 (2026-09-14) THE REFERENCE MEASUREMENTS, RE-VERIFIED INDEPENDENTLY.**
  Every number in MASTER_PLAN §1 was re-measured from the two source files this
  session. Reproduced exactly: 576x1024 @ 24.000fps, 2094f/87.28s (clip A, New
  Girl) and 1495f/62.29s (clip B, Reacher); band geometry within 3px; 41 and 14
  cuts by coarse mean-delta. THREE CORRECTIONS came out of it:
  1. **The plan's normalised 1080x1920 layout table did not close.** It gave
     16:9 as 321+608+71+608+313 = **1921**, one pixel over the canvas, and its
     asymmetric margins contradicted its own `block_centered` rule. Margins are
     now DERIVED, never tabulated (see D42).
  2. **Clip B's mean shot length is 4.15s, not 2.35s.** 14 cuts in 62.29s is 15
     shots. The correction STRENGTHENS the case for adaptive keyframing: a
     0.46s median against a 4.15s mean means clip B is mostly long dialogue
     holds punctuated by very short bursts.
  3. **Cadence measured 11.1/11.2 effective fps** with the 2-frame gap dominant
     in both clips (150/219 and 139/222 changed-frame gaps). On-twos confirmed
     to the frame. 12fps is correct and must not be changed.
- **D42 (2026-09-14, T9) THE CANVAS IS DERIVED FROM THE SOURCE'S ASPECT RATIO,
  NOT CONFIGURED.** `render.header_height`, `panel_height` and `divider_height`
  are DELETED from styles.yaml and RenderConfig, along with the
  `_geometry_closes` validator. `claypipe/pipeline/layout.py` computes:
      panel_h   = even(canvas_width / source_aspect)
      gap       = even(gap_fraction * canvas_height)
      remainder = canvas_height - 2*panel_h - gap
      top       = even(remainder / 2)      <- the header bar is drawn here
      bottom    = remainder - top
  16:9 -> 316/608/72/608/316. 2.014:1 -> 388/536/72/536/388. Both sum to 1920,
  every term even (yuv420p subsamples chroma 2x2). Verified on RENDERED PIXELS,
  not on config.
  **Why this is not a refactor:** the old fixed 882px panel cropped real picture
  away from BOTH reference aspects. The crop in `_panel_filter` is now a no-op
  for a normal run and stays only as the safety net for a forced aspect.
  `RunManifest.source_width/height` became run identity, probed once at intake.
  A run.json written before T9 has none, so `layout_for_run` re-probes and logs
  `assemble.layout.reprobed` — never a canvas-shaped guess, because a wrong
  aspect crops picture and still produces a video that plays fine.
  `caption_gap_fraction` (0.037) replaced `divider_height`: the caption band is
  a first-class layout band now, because T12 draws captions INTO it.
- **D43 (2026-09-14, T10) IDENTITY REFERENCES ARE THE APPROVED CANARY OUTPUT,
  NOT SOURCE STILLS. This would have broken the first paid run.** MEASURED on a
  real 20s slice of clip A — restyled frames scored against each kind of
  reference:
      vs CANARY refs : 0.907  0.867  0.958  0.839
      vs SOURCE refs : 0.646  0.535  0.655  0.579    (targets.id_min = 0.85)
  Every source-referenced frame MISSES the target. The failure chain was:
  miss id_min -> BORDERLINE (D13) -> retry at strength -0.10 (D18) -> 15% retry
  budget exhausted around frame 107 -> the backend gets blamed. The plan
  predicted 0.65-0.82 for the photoreal case; the measurement is 0.53-0.65,
  WORSE than predicted — and this is the dummy backend, which barely alters the
  picture. A real restyle moves CLIP embeddings further, so the effect grows.
  `canary submit` on a FULL approval copies the approved frames into `refs/` and
  sets `reference_origin="canary"`. "adjust" locks nothing (the look is about to
  change, so locking it locks the wrong target); rejection locks nothing.
  Individually rejected frames are excluded; a re-submission REPLACES the lock
  rather than accumulating, or the ID target averages the approved look against
  a superseded one.
  **DO NOT "FIX" THIS BY LOWERING id_min.** That trades a calibration bug for a
  blind gate. An intake-origin reference now WARNS at startup (naming the
  consequence and the fix) rather than refusing — holding a restyle to a
  photoreal reference is a legitimate if expensive choice, but it must be a
  choice, not a default discovered at frame 107.
  **D12 CLOSED by this.** "How does a style with no character (e.g. `logo`)
  score identity?" — against the frames the operator approved. No character
  needed, no special case.
- **D44 (2026-09-14, T10a) `claypipe canary restyle` — the stage the gate was
  always meant to sit in front of.** D17 puts the canary gate at the `batch`
  command boundary, but a verdict needs frames to look at, and the only way to
  get them was to run the full batch FIRST — paying for every frame of a look
  nobody had approved. `canary restyle` restyles ONLY the three canary frames,
  through the ledger. On fal Kontext pro that is $0.12 instead of $28.80.
  Deliberately UNSCORED: no identity references exist yet, by definition (D43),
  and a score with no reference is a number with a hole in it. It uses the shot
  plan's seed so the operator approves a look the batch actually reproduces, and
  the batch RESUMES those 3 frames rather than regenerating them (asserted:
  resumed=3, restyled=57, total=60 — no double spend).
- **D45 (2026-09-14, T11) SHOT DETECTION IS THE KEYSTONE, AND
  PySceneDetect-ABSENT IS A REFUSAL, NOT A FALLBACK.** `claypipe/pipeline/
  shots.py`. Three problems collapse into it:
  1. **Seeding.** "Per-shot fixed seed" was specified and has been per-CLIP
     since step 1. `seed = 1000 + shot_index`.
  2. **D15/D34 CLOSED STRUCTURALLY.** A hard cut looks EXACTLY like
     catastrophic temporal failure to a flow-warped residual, because the
     previous frame is a different scene and the warp measures nothing.
     `restyle_frames(boundary_frames=...)` now hands the scorer
     `previous_restyled=None` at a shot opener, so the frame is scored as the
     first frame it effectively is — reusing existing semantics rather than
     special-casing the metric. `scores.jsonl` records `shot_boundary` per
     frame so a reviewer can see WHY a TF is the first-frame value.
  3. **D30 CLOSED.** The canary's third slot was the LAST frame as a stated
     stand-in. It is now the MIDPOINT of the busiest shot — midpoint, not
     opener, because a cut's first frame is often the calm instant before the
     action it was cut to. Motion is ranked over the SOURCE frames: the
     operator is choosing which moment to inspect, and that must not depend on
     what the backend already did to it.
  Detection runs on the source at ITS OWN frame rate then maps to extracted
  numbering. Those are different clocks — a 24fps source extracted at 12fps has
  half the frames — and conflating them puts every boundary in the wrong place
  silently. `_assert_covers` proves every extracted frame belongs to exactly one
  shot; the alternative failure is a raise hundreds of frames into a paid batch.
  A missing PySceneDetect raises with the install line. It must NEVER fall back
  to one whole-clip shot: that silently restores per-clip seeding and the false
  TF flags, which is the bug. `--single-shot` is the explicit escape hatch and
  logs a warning naming what it disables.
  **DETECTOR CALIBRATION (measured):** clip A -> 41 cuts, reproducing the
  independent coarse mean-delta measurement EXACTLY; 28.9 shots/60s, and this is
  the clip the keyframe budget is set against. Clip B -> **21 cuts, against 14
  from the coarse pass.** The two detectors genuinely disagree; clip B is dark
  high-contrast action where a luma threshold under-counts cuts between
  similar-looking shots. Recorded as 21 WITH the disagreement stated, not
  averaged away.
- **D46 (2026-09-14, T12) CAPTIONS ARE A LAYOUT ELEMENT IN THE GAP BAND. THE
  `subtitles=` FILTER IS GONE.** The reference clips put captions in the band
  BETWEEN the panels, never over the picture (MASTER_PLAN §1.4).
  libass draws over the composited frame and has no idea the gap exists, so a
  long cue at a fixed size spills onto a panel and crops a face — in a file that
  plays perfectly. The gap is 72px of 1920; fitting text to it is a measured
  constraint and Pillow can measure. A subtitle filter cannot be asked "did that
  fit". This also means ONE text renderer for the whole canvas, alongside D1's
  header.
  The band is now its own TRACK: a PNG sequence, one gap-band-sized RGBA image
  per output frame, composited as a single FINITE input at the gap's y offset.
  NOT one overlay per cue — dozens of still inputs and `enable=between(...)`
  filters would flirt with the unbounded-input trap D5 records. Blank frames are
  REAL IMAGES: a PNG sequence with a missing index stops the input early, which
  truncates the overlay and, via D6's trim bound, the video.
  Verified: audio MD5 `a8dc9027aae8ddf557b0f1a09a4c7336` identical on the
  extracted `audio.aac`, on the output without captions, and with them. Caption
  ink on a real render spans y=942..979 inside the 924..995 band.
  **Non-dialogue cues are PRESERVED and rendered but never INVENTED.** Whisper
  transcribes speech; labelling `*smack*` needs an audio event classifier this
  pipeline does not have. `cues.json` is therefore hand-editable and `claypipe
  captions` refuses to clobber it without `--overwrite`. `sentence_case` leaves
  `[dramatic music]` exactly as written — a full stop would corrupt a
  closed-caption convention.
- **D47 (2026-09-14) TWO BUGS FOUND BY PROVING THE FLOWS END TO END, not by
  reading code.** Recorded because both produced output that looked fine:
  1. **Canary slot collision.** On a single-shot clip the most-motion frame IS
     the middle frame, so the selection silently returned TWO frames for a
     three-frame canary — the operator would approve less than they were quoted
     for. Collisions now fall through to the next distinct candidate in the same
     busiest shot, with an assert that three distinct frames come back.
  2. **A 3-frame canary page captioned its third slot "no shot plan for this
     run"** when the plan had already been applied by `canary restyle`.
- **D48 (2026-09-14) THE SYNTHETIC FIXTURES CANNOT DEMONSTRATE F1 — and that
  contrast IS finding F4.** On the bundled `testsrc` clip the dummy posterise
  barely moves a CLIP embedding, so the canary-vs-source ID gap collapses to
  **0.001** and the fixture "proves" F1 is a non-issue. On real footage the same
  measurement gives **0.25-0.35**. The F1 measurement test therefore SKIPS
  unless `CLAYPIPE_REFERENCE_CLIP_A` points at real footage, with the reason in
  its docstring. Generalisation: thresholds and premises calibrated on
  deterministic synthetic fixtures do not transfer to generative output on real
  frames. T16 is the first real datum; do not treat fixture numbers as
  calibration.

- **D49 (2026-09-14, T13/T15) THE SCORING GATE AND THE PRICE MODEL ARE BOTH
  MODE- AND UNIT-DEPENDENT.**
  `modes:` in weights.yaml carries a target vector AND a retry policy per track.
  `modes.surface` restates the canonical `targets`/`firewalls` numbers exactly
  and a validator refuses a mismatch on any of the six — the restatement exists
  so Track C can differ, not so the two can drift (Rule 31).
  The resynth difference is NOT "looser everywhere": it relaxes ssim/lpips/id
  and **TIGHTENS tf_min to 0.97**, because temporal consistency is what Track C
  is bought for. A resynthesis model that flickers has no reason to exist.
  Per-mode retry policy for a structural reason (A4): a failed FRAME reseeds one
  frame, a failed CLIP CHUNK reseeds 81-240 frames. resynth gets
  `max_retries_per_unit: 1` (not 2) and a 5% budget (not 15%).
  **NEW F4 FIREWALL: `resynth.calibrated: false`, and FIREWALL 0b refuses a PAID
  run in an uncalibrated mode.** Its numbers have the right shape and no
  evidence. The refusal names T16 as the way out. An uncalibrated mode must
  carry a `calibration_note` so a future session can tell a placeholder from a
  measurement. **T16 must set these numbers and flip the flag in the SAME
  commit that records the decision.**
  T15 pricing: `{unit: image|megapixel|video_second, rate, round_up_to_mp}`.
  Verified 60s of Wan VACE 480p prices at exactly $2.40. `round_up_to_mp` is
  fal's real billing rule and INVERTS an optimisation — 0.41MP (640x640) bills
  as 1MP, so on a hosted per-MP backend render LARGE, and on self-hosted GPU
  (cost linear in pixels) render SMALL. Render geometry therefore cannot be a
  fixed aesthetic constant.
  Three REFUSALS rather than conveniences: `per_call()` on a non-per-image
  backend raises instead of answering; pricing a unit backend without its
  dimension raises; an unpriced backend is still refused. The ledger records the
  unit and billed dimension, because an entry saying only "$0.05" cannot tell a
  2-megapixel image from two 1-megapixel ones.
- **D50 (2026-09-14, T14) A TRACK C RUN CANNOT BE APPROVED BY THREE STILLS, AND
  THE PLAN'S 3-SECOND/$0.12 VACE CANARY IS NOT PURCHASABLE.**
  `canary restyle --clip <seconds>`. FIREWALL 1b refuses a resynth run whose
  canary was the wrong kind, and `canary restyle` without `--clip` refuses
  outright in resynth mode — the wrong kind can be neither produced nor consumed.
  **`canary_kind` lives on the MANIFEST, not in the verdict.** The verdict
  arrives as a query string the operator pastes (D21), so a gate satisfiable by
  editing a URL is not a gate (D17). There is a test that forges `canary_kind`
  in the verdict and asserts the gate still holds.
  The trim is anchored on the MOST-MOTION shot, not the clip head: a video
  model's failure mode is temporal, and the opening seconds are often a static
  establishing shot where none of it shows. Canarying the calm part of a clip is
  how a temporal model passes a gate it should fail.
  **CORRECTION TO THE PLAN:** a 3-second Wan VACE canary is neither purchasable
  nor $0.12. VACE's floor is 81 frames at 16fps native = **5.06 video-seconds =
  $0.20**. A 3-second canary at the pipeline's 12fps is 36 frames, which VACE
  cannot honour — it bills its minimum, or pads, and padding changes the frame
  count and breaks the duration invariant. Asking below the floor buys a
  SMALLER canary at the SAME price. T14 warns with that arithmetic.
  `DummyClipBackend` is deliberately INCONVENIENT — 16fps native against the
  pipeline's 12, 81-240 frames per chunk — because those two facts are what
  force the duration invariant (A2/T17). A convenient stand-in would let the
  pipeline pass tests it should fail. It also MOVES GEOMETRY, or it would
  exercise the surface gate instead of the resynth one.
  `ChunkLengthError` is the failure with no per-frame analogue: a backend
  returning 80 frames for 81 shortens the clip and desyncs the audio, in a file
  that plays perfectly.
  `dummy_clip` had to be PRICED in weights.yaml ($0/video-second). Not a
  formality — an unpriced backend is refused outright, so an unlisted
  dummy_clip made the whole Track C path untestable. The firewall caught this
  on the first end-to-end run.
- **D51 (2026-09-14, T18) TEMPORAL FIDELITY CANNOT VALIDATE PROPAGATION — TF
  GETS *BETTER* WITH LONGER WARP CHAINS.** This is the most important finding of
  the session and it contradicts T18's own acceptance criterion.
  Measured on a real 60s slice (720 frames, 28 shots), DummyBackend keyframes:
  | max_chain | paid | reduction | TF min | TF mean | % >= 0.99 |
  |---|---|---|---|---|---|
  | 6 | 142 | 5.07x | 0.8833 | 0.9905 | 75.4% |
  | 12 | 96 | 7.50x | 0.8784 | 0.9921 | 79.0% |
  | 24 | 74 | 9.73x | 0.8951 | 0.9932 | 83.0% |
  | unbounded | 59 | 12.20x | 0.9216 | 0.9947 | 84.4% |
  **Why:** a warped frame is by construction a smooth resampling of its
  predecessor, so it is almost perfectly temporally consistent. A KEYFRAME is a
  fresh generation that does NOT match its predecessor. Every keyframe
  *insertion* is a temporal discontinuity, so fewer keyframes = better TF.
  So TF does not measure propagation drift; it measures how often a chain is
  interrupted. **T18's "propagated frames score TF >= 0.99" is NOT MET (84.4%
  at best) and is the WRONG CRITERION.** What catches smear is fidelity TO THE
  SOURCE (SSIM / LPIPS-edges), not frame-to-frame consistency. Do not "fix"
  this by tightening tf_min.
  T18's other criterion IS met: **59 paid frames of 720 (12.2x)**, under the
  <=60 target, with an unbounded chain. The residual threshold does the real
  work (177 paid at 0.03 -> 85 at 0.07, then saturates; 0.055 is the knee).
  SSIM-vs-source also turned out FLAT in chain depth (0.367 at the keyframe,
  0.315 at depth 40-44), so max_chain earns little on this footage.
  **THE DEFAULT IS STILL max_chain=12, AND THE REASON MATTERS:** both
  measurements used DummyBackend, whose output is flat posterised colour fields.
  Resampling a flat field is nearly lossless, so the dummy CANNOT show the drift
  repeated resampling would cause in real clay texture, fingerprints and tool
  marks — its evidence that long chains are safe is weakest exactly where it
  counts. 12 buys 7.5x ($3.84/clip vs $28.80 on Kontext pro).
  **RE-MEASURE AT T16 ON A REAL BACKEND BEFORE RAISING IT.**
  Also: the absolute SSIM figures (0.33-0.37 against a 0.72 target) are NOT a
  propagation failure — that is the dummy backend scoring against real footage.
  The same posterise scores above 0.72 on the synthetic fixtures. D48/F4 again.
- **D52 (2026-09-14) A CAPTION BUG THAT ONLY LOOKING AT A FRAME COULD FIND.**
  A two-line cue fitted to the full 72px gap height rendered EDGE TO EDGE (ink
  rows 6..71) — technically inside the band, visually crossing into both panels,
  which is the exact failure T12 exists to prevent. `assert_within_gap` PASSED
  it, because it only checked containment.
  Cause: TWO line-height definitions. `_fit_font` measured `getbbox("Ay")` (ink
  extent), `render_cue_image` used `getmetrics()` (ascent+descent, taller). The
  fitter approved a size that then rendered taller than it had measured. There
  is now one `line_height_for` used by both.
  Fixed on top of the cause: `CAPTION_VERTICAL_PADDING` (14%) so text CLEARS
  both panels, and `assert_within_gap` now checks CLEARANCE, not containment.
  **Lesson worth keeping: the end-to-end visual check is part of the work, not
  a formality. No unit test caught this and the assertion actively passed it.**

- **D53 (2026-09-15) THE T16 BAKE-OFF, VERIFIED AGAINST fal's LIVE MODEL PAGES.
  THE BUDGET IS WRONG AND ONE CANDIDATE IS UNBUYABLE.** Every price below was
  read off fal's own model page on 2026-09-15, and each `weights.yaml` entry
  names the endpoint it came from so a future session re-checks one line
  instead of re-researching the table.
  | Candidate | fal endpoint | Price | Unit | Canary |
  |---|---|---|---|---|
  | Kontext [pro] | `fal-ai/flux-pro/kontext` | $0.04 | image | $0.1200 |
  | Kontext [dev] | `fal-ai/flux-kontext/dev` | $0.025 | MP, **rounds up** | $0.0750 |
  | Qwen Image Edit | `fal-ai/qwen-image-edit` | $0.03 | MP | $0.0900 |
  | Flux gen + CN + LoRA | `fal-ai/flux-general/image-to-image` | **$0.075** | MP, rounds up | **$0.2250** |
  | SDXL + ControlNet | `fal-ai/fast-sdxl-controlnet-canny/image-to-image` | **per COMPUTE SECOND** | UNSUPPORTED | — |
  | Wan VACE 480p | `fal-ai/wan-vace-14b` | $0.04 | video_second | **$0.2025** |
  | Runway Aleph | deferred | $0.18 | video_second | $0.9113 |
  **FOUR FINDINGS, in order of how much they change things:**
  1. **`fal` WAS UNDER-PRICED IN OUR OWN CONFIG (0.035 vs the real 0.04), and
     that would have refused every real call.** The configured estimate is a
     CEILING (`FalBackend.assert_affordable`), so a price below the true one
     makes the firewall reject the endpoint's quote. Fixed. This is the single
     most useful thing the verification caught.
  2. **SDXL + ControlNet on fal is billed PER COMPUTE SECOND — a fourth unit
     T15 does not implement, and CANNOT implement honestly.** The ledger
     authorises BEFORE the call by design; a compute-second charge is unknown
     until after it. So it can be capped but never pre-authorised. It is
     therefore refused as unpriced, which is the CORRECT outcome, not a gap to
     paper over. The SDXL candidate is consequently **untestable on fal**, for
     a different reason than SD1.5 is.
  3. **The LoRA candidate costs 7.5x what the brief assumed.**
     `fal-ai/flux-general/image-to-image` is the ONLY fal endpoint taking both
     `controlnets` and `loras` (by URL, confirmed: "URL or the path to the LoRA
     weights"), so it is the sole way to test structure-conditioning plus a
     claymation LoRA together — but at $0.075/MP it is $0.225 for three frames,
     not the ~$0.03 assumed. Note it is FLUX-based, so it needs a Flux clay
     LoRA, not an SDXL one.
  4. **The 5.06s VACE floor is CONFIRMED FROM THE SCHEMA**, not inferred:
     num_frames "must be between 81 to 241 (inclusive). Default value: 81", and
     "Video seconds are calculated at 16 frames per second". 81/16 = 5.0625s =
     $0.2025. `DummyClipBackend` now mirrors 81-241 exactly (was 81-240).
  **CORRECTED BUDGET.** The amendment's $0.53 cap does not cover the run it
  describes. Buyable total with the LoRA candidate: **$0.7125**. Without it:
  **$0.4875**, but then nothing tests a clay LoRA. Both numbers are below the
  $10 balance either way; the cap is the operator's call, not a code decision.
  **NOT CODE, AND STILL OPEN:**
  - **B1 was NOT re-confirmed.** The first version of this session's brief said
    "the key in `.env` is new, not the one disclosed in D36. Proceed." The
    re-sent version has that line DELETED. Treated as unconfirmed rather than
    assumed; T16 is a paid run and D36 is a disclosed-key incident.
  - **A claymation LoRA has not been chosen.** The brief asks for one matching
    the endpoint's base, and that base is FLUX, not SDXL.
- **D54 (2026-09-15, T9a) THE LAYOUT HAS TWO BRANCHES, AND THE CUTOFF IS
  DERIVED.** A square source makes two full-width panels 2*1080 + 72 = 2232px
  against a 1920 canvas: the width-fit rule has no solution and quietly derives
  a negative margin. Both reference clips were widescreen so this was never
  exercised; the Young Sheldon clip (640x640, true square, confirmed by
  cropdetect) is the first that hits it.
      WIDTH-FIT   panels span the canvas, height follows the aspect (unchanged)
      HEIGHT-FIT  panels fit the vertical space, width follows the aspect, and
                  they are centred with background pillarboxing
      cutoff = W / ((H - gap - 2*min_margin) / 2)  = 1.4362
  `MIN_MARGIN_FRACTION = 0.09` (172px of 1920) is constrained from BOTH sides
  and is not reverse-engineered from a desired cutoff: it must clear
  MIN_HEADER_HEIGHT (96px legibility) and stay under the margins the reference
  format actually uses (16.6% clip A, 20.6% clip B), so it binds only on
  aspects those clips never covered. Both constraints are tests.
  Verified on RENDERED PIXELS of the square clip: vertical 172/752/72/752/172,
  horizontal content x=164-915 with exactly 164px background each side.
  The branches meet CONTINUOUSLY — at the cutoff the height-fit panel is
  exactly canvas width — so two clips differing by 0.01 of aspect do not jump
  size. Asserted.
  Height-fit deliberately does NOT stretch panels to canvas width; that naive
  fix distorts every face while leaving a file that plays.
- **D55 (2026-09-15, T9b) BURNED-IN TEXT IS DETECTED AT INTAKE, AND NEITHER THE
  POSITION NOR THE DISCRIMINATOR IS THE OBVIOUS ONE.** Two wrong assumptions
  were corrected by measurement:
  1. **NOT the lower third.** The brief proposed looking there (where broadcast
     subtitles live). The Sheldon band peaks at **row 327 of 640 — 51% down,
     dead centre** — because short-form social captions are centred. A
     lower-third detector reports that clip CLEAN. The band is now searched for
     across the full height.
  2. **PERSISTENCE IS THE WRONG DISCRIMINATOR and the first implementation got
     it exactly backwards because of it.** Sheldon's caption rows score
     0.29-0.33 persistence (captions change text, blink, shift with line
     count); the reference clips' TikTok WATERMARK scores **0.89**, because a
     watermark is pinned. So v1 missed the clip that has captions and fired on
     the two that only have a watermark.
  What works: **outlined-bright density** — bright fill with a DARK pixel within
  three on the same row. Every legible overlay is drawn that way to survive any
  background; a blown-out window is bright with nothing dark beside it. Unit
  test: a synthetic glyph fires, a synthetic highlight does not.
  Measured, all four correct: Sheldon FIRES as `captions` (rows 297-342/640, 50%
  down, 52% of width); clip A FIRES as `watermark` (93% down, 11% of width);
  clip B FIRES as `watermark` (74% down, 22% of width); synthetic testsrc quiet
  (band spans 43% of height = picture content). Clips A and B are TRUE
  positives — a watermark also gets restyled.
  Also fixed: horizontal extent was measured with longest-CONTIGUOUS-run, but
  glyphs have gaps between letters, so it reported 3px for a band spanning 62%
  and misclassified every caption as a watermark. Extent (first lit column to
  last) is correct. And it probes at 640px, not 320 — downscaling thins strokes
  until the outline signal disappears.
  ADVISORY, never a block. `--allow-burned-captions` records acknowledgement.
  A clean source records `detected: false` WITH a reason, so "checked and
  clean" and "never checked" stay distinguishable (Rule 40).
- **D56 (2026-09-15, T18a) TF ON A PROPAGATED FRAME IS CIRCULAR, SO UNTIL NOW
  PROPAGATION HAD NO DRIFT CHECK AT ALL.** The operator's diagnosis is sharper
  than D51's and supersedes its explanation: it is not merely that "TF measures
  how often a chain is interrupted". A propagated frame **is** a warp of its
  predecessor along the optical flow, and `temporal_fidelity` scores a frame by
  warping its predecessor along the optical flow and differencing. **The metric
  and the generation method are the same operation.** TF measures its own
  assumption, and scores well precisely because the frame was made by the
  process doing the grading. Meanwhile T18 was already wired into `batch` and
  already shipping a 5-12x spend reduction.
  `propagate.score_drift` scores propagated frame N against SOURCE frame N on
  SSIM and LPIPS-edges and **never touches the flow field** — there is a test
  that patches `flow_between` and asserts zero calls, because inheriting the
  flow would inherit the circularity. Another test DEMONSTRATES the
  circularity: ten chained warps score TF > 0.97 while source-referenced SSIM
  is materially lower.
  Sampled (LPIPS is a forward pass per frame) and **stratified BY CHAIN DEPTH**,
  because the question is "does drift grow with depth" and a uniform sample
  under-represents the deep chains that are the entire risk. That stratification
  is what makes T16's max_chain sweep answerable.
  **Reported beside F, never inside it.** No calibrated threshold for
  source-referenced drift exists until T16 measures one; inventing one would be
  F4 with a new name. `null` means "did not propagate", not "drift zero".
  **DUMMY NULL CONTROL** on the full 711-frame Sheldon clip (143 paid / 568
  warped / 4.97x):
      depth  0-3  n=15  SSIM 0.3659  LPIPS-e 0.4696
      depth  4-7  n=20  SSIM 0.3252  LPIPS-e 0.4335
      depth 8-11  n=20  SSIM 0.3573  LPIPS-e 0.4524
      depth 12-15 n=5   SSIM 0.3101  LPIPS-e 0.4290
  FLAT AND NON-MONOTONIC — LPIPS actually IMPROVES with depth (-0.041), which is
  how you can tell it is noise, not drift. The expected NULL RESULT: the dummy's
  flat posterised fields resample near-losslessly and cannot show the smear real
  clay texture would suffer. **It proves the instrument works; it does not
  validate propagation.** The CLI prints that caveat itself.
  `max_chain` stays at 12. It must not move on dummy-backend evidence.
- **D57 (2026-09-15) RUNBOOK.md AND CONFIG.md EXIST, AND A TEST NOW ENFORCES
  RULE 33.** Saying "docs must match code" in a memory file did not stop
  `status` carrying a stale caption line for a whole session, so
  `tests/test_docs_match_code.py` checks the CLAIMS rather than the prose:
  every `claypipe <cmd>` and `--flag` RUNBOOK names must resolve; every
  `RunPaths` artefact must appear in its artefact table; every leaf key in both
  YAML files, every priced backend and every style profile must appear in
  CONFIG.md; CONFIG.md must not TABULATE the three geometry keys T9 retired
  (listing them as settable would send an operator to set a value that now
  crashes startup under `extra: forbid`); it must state the derived layout
  cutoff computed FROM the code; and it must not be readable as saying resynth
  is ready to spend.
  It caught six real gaps in CONFIG.md on its first run (four Farneback
  parameters, both `calibration_note` fields, a missing "NOT CALIBRATED"), and
  then caught an undocumented backend one commit later when D53's prices landed.
- **D58 (2026-09-15) TARGET_DNA `shots` IS NOW A MEASURED RANGE WITH
  PROVENANCE, NOT A POINT VALUE — AND THE DETECTORS DISAGREE.** Three source
  clips, all measured with PySceneDetect ContentDetector at the shipped
  threshold 27.0, which is what the code actually uses:
  | Clip | Frames | Duration | Cuts | Shots | Mean | Median | Shots/60s |
  |---|---|---|---|---|---|---|---|
  | A New Girl 576x1024 | 2094 | 87.28s | 41 | 42 | 2.08s | 1.62s | 28.9 |
  | B Reacher 576x1024 | 1495 | 62.29s | 21 | 22 | 2.83s | 2.42s | 21.2 |
  | C Young Sheldon 640x640 | 1421 | 59.35s | 25 | 26 | 2.28s | 2.21s | 26.3 |
  Observed span **21-29 shots per 60s**, mean shot **2.1-2.8s**.
  **This is NOT the brief's ~14-32 / 1.9-4.2s range, and the difference is
  detector choice, not measurement error.** The brief's Sheldon figure (30 cuts,
  31.4/60s, mean 1.91s) and its clip B figure (14 cuts) come from a coarse
  mean-delta pass; PySceneDetect finds 25 and 21 respectively. The two
  genuinely disagree on dark high-contrast action. **The range recorded here
  uses the detector the pipeline ships**, because that is the one that will
  produce the keyframe budget. Budget against clip A at 28.9/60s, the densest
  under this detector.

- **D59 (2026-09-16, V1) fal's Wan VACE BILLS FRAMES/16, NOT WALL CLOCK.**
  Verified verbatim on the model page AND llms.txt: "Video seconds are
  calculated at 16 frames per second." A 60s clip at our 12fps cadence is 720
  frames = 45 billed seconds = **$1.80** at 480p, not the $2.40 wall-clock
  implies. At 24fps the same minute is $3.60 — so the CADENCE moves the bill
  even though duration does not. New `frames_div_16` unit sits alongside
  `video_second` (Qwen Cloud's Wan 3.0 bills wall-clock); each REFUSES the
  other's dimension rather than silently mispricing.
  `BILLED_FRAMES_PER_SECOND = 16` in config.py carries the quote.
- **D60 (2026-09-16, V2) THERE IS NO CANNY ON VACE. ANYWHERE.** fal's `task`
  enum is `depth | pose | inpainting | outpainting | reframe` — verified on the
  wan-vace-14b API page, its llms.txt, and wan-22-vace-fun-a14b; the deprecated
  wan-vace has only depth+inpainting. No lineart, no scribble, and no
  passthrough for a pre-processed control video. **The silhouette-locking
  signal the plan assumed is not purchasable at any price**, so the three-way
  canary was always a two-way one. Requesting canny raises rather than silently
  substituting depth.
  Also verified: num_frames 81-241 inclusive, frames_per_second 5-30,
  resolutions auto|240p|360p|480p|580p|720p (only 480/580/720 are priced).
- **D61 (2026-09-16, V3) PROPAGATION RETIRED, AND THE GHOSTING DEFECT DELETED
  WITH IT.** A video model bills per frame or per second whether or not frames
  were warped, so propagation saves nothing under v2v and costs fidelity. With
  no warped frames, nothing can inherit a stale caption from a keyframe — D52's
  defect is gone rather than mitigated. `surface` is the retired track, kept one
  release.
  **A sequencing note worth keeping:** flipping intake's default to `resynth`
  before V5 built the resynth batch path broke 15 tests, and the cause was not
  the tests — the default mode could not complete a batch. The fix was a test
  asserting the INVARIANT (whatever mode is default must complete a batch end
  to end) rather than the value, so the flip could not happen early and could
  not be forgotten.
- **D62 (2026-09-16, V4) FLOW IS THE PRIMARY GATE; TF IS NO LONGER CIRCULAR; ID
  IS SCOPED TO CHARACTER REGIONS.**
  FLOW = 1 - normalised endpoint error between the output's flow field and the
  SOURCE's. Validated before wiring: identical motion 1.0000, half-speed 0.5292,
  frozen 0.3270, reversed 0.0000. **It is not TF** — a frozen output scores TF
  1.0000 and FLOW < 0.6. It already earned its place by reporting 0.00-0.40 on
  the retired propagation output, catching the desync T18a's whole-frame SSIM
  was structurally blind to.
  **TF's circularity was propagation-specific** (a warped frame graded by a
  warp). V3 retired propagation, so TF measures a real property again. **Do not
  re-flag it.**
  ID scoped to character regions, because under whole-frame restyle the
  environment is rebuilt in clay too and dominates a whole-frame embedding.
  **A CROP MUST BE COMPARED AGAINST A CROP** — my first attempt scored crops
  against whole-frame references, which asks CLIP whether a person resembles a
  scene; it depressed ID enough to miss id_min on most frames, exhaust the
  retry budget and halt the run. Region references are now cropped from the
  approved canary, and ID falls back to whole-frame with the support LABELLED
  (`regions:N` / `whole_frame` / `whole_frame:no_region_refs`).
  **No thresholds set.** `flow_min` is 0.0 — report, do not gate — with a test
  asserting it stays 0.0 until a canary measures one. FLOW is a COMPONENT
  TARGET, not a term in F: re-weighting the composite would invent a weight for
  an uncalibrated metric.
- **D63 (2026-09-16, V5/V6) ONE SEED PER RUN, SEAMS ON CUTS, AND TWO KINDS OF
  SECOND.**
  **One seed for the WHOLE RUN, inverting T11.** Per-shot seeding was right for
  img2img (seed drives retry variety); under v2v the seed drives the generated
  DESIGN, so changing it between chunks redesigns the character at every seam.
  Chunks accumulate whole shots so seams land on cuts, where a design shift is
  invisible. On the Sheldon clip: 711 frames, 26 shots -> 4 chunks, 3 seams,
  ZERO mid-shot. A forced mid-shot seam is REPORTED, never silent.
  **The units trap:** VACE's 81-frame floor is 6.75 TIMELINE seconds but 5.0625
  BILLED seconds. Typing `--clip 5.06` buys 61 frames — under the floor, billed
  at the floor anyway. `--clip-floor` reads the minimum off the backend and
  prints both figures.
  **A hard product constraint:** a source under 81 frames (6.75s at 12fps)
  cannot be canaried or batched at all. The bundled test clip went 5s -> 8s for
  this reason.
- **D64 (2026-09-16, V7) THE CANARY RAN. VACE RETURNS A COLOUR GRADE, NOT CLAY.
  $0.6075 SPENT. DO NOT PROCEED TO V8 ON THIS CONFIGURATION.**
  Two signals, 480p, 81 frames each, on the Sheldon clip:
  |        | FLOW | TF | SSIM | LPIPS-e |
  |---|---|---|---|---|
  | depth | 0.7358 | 0.9735 | **0.9050** | 0.1534 |
  | pose | 0.7057 | 0.9748 | **0.8546** | 0.2370 |
  Both returned the same photoreal footage with a heavy orange/teal grade —
  same faces, same geometry, same composition, no clay texture.
  **SSIM 0.905 is the damning number: the RETIRED img2img gate demanded 0.72,
  so this "resynthesis" preserves structure BETTER than the per-frame path was
  ever asked to.** A model scoring 0.85-0.91 against its own input has not
  resynthesised anything.
  ID deliberately not reported: it scores against the APPROVED canary (T10/F1)
  and nothing is approved — measuring against the source would reproduce the
  photoreal-reference bug F1 documents.
  **THE ONE UNTESTED CONFOUND IS OURS, NOT THE MODEL'S.** The `clay` prompt
  ends "keep exact same composition, pose, camera angle, framing and colors" —
  written for img2img, where preserving the frame was the point. Under
  whole-frame v2v it instructs the model to change nothing, INCLUDING COLORS,
  which is close to a description of what came back. **Try the prompt before
  concluding VACE cannot do clay.** It is the cheapest variable and it is
  untested.
  Escalation options, in cost order: rewrite the prompt (~$0.20 to retest);
  580p/720p (~$0.30/$0.41 per canary); Runway Gen-4.5 Aleph ($0.91 for the same
  trim, $10.80/minute).
- **D65 (2026-09-16) A PAID GENERATION WAS LOST TO A TRUST-STORE MISMATCH.**
  The download used urllib (SYSTEM trust store) while fal_client uses httpx
  (certifi). Behind TLS interception the upload and the generation both
  SUCCEEDED and only the download failed — charged $0.2025 for a result never
  retrieved. Now downloaded with httpx, and the error says the spend is in the
  ledger as authorised-but-unreconciled. The stranded entry is PRESERVED as
  `spend_ledger.stranded.jsonl`.
  **Generalisation worth keeping: any code path that spends money must use the
  same HTTP client as the SDK that spent it**, or a trust/proxy difference
  turns a successful purchase into a lost one.
- **D66 (2026-09-16) C4 DID NOT EXIST, THOUGH THE BRIEF SAID IT DID.** §3 listed
  caption inpainting as surviving and §6 said "C4 already handles it"; there was
  no inpainting anywhere and no C4 task in memory.md or MASTER_PLAN.md. T9b only
  DETECTS. Built it: Telea inpaint over the T9b band, applied to the GENERATOR'S
  INPUT only — the original panel keeps its captions, the audio is untouched.
  Deliberately cheap, because its output feeds a model about to restyle the
  whole frame and stylisation covers the artifacts.
  The band had to be SCALED out of T9b's probe space (fixed 640px width) or it
  masks the wrong strip on every frame, and grown 25% because outlines extend
  past the detected ink.
  **And it had to be wired into the CANARY too, not just batch** — the first
  canary judged raw frames and its output still carried the captions production
  would have stripped. A canary that does not see what production sees is not a
  canary.

## Pending / Next
- **THE DECISION IN FRONT OF THE OPERATOR (D64): V8 is NOT recommended on this
  configuration.** VACE returned a colour grade. In cost order:
  1. **Rewrite the `clay` prompt and retest (~$0.20).** Cheapest, and the only
     untested variable that is ours. Strip "keep exact same composition, pose,
     camera angle, framing and colors" — it is an img2img instruction telling a
     v2v model to change nothing.
  2. **Higher resolution** (~$0.30 at 580p, ~$0.41 at 720p per canary).
  3. **Runway Gen-4.5 Aleph**, $0.91 for the same 5.06s trim, $10.80/minute.
     The quality ceiling, priced accordingly.
- **Balance check before any further spend.** $0.6075 of the ~$10 fal balance
  is gone. A full V8 run at 480p would be $1.80 (720 frames / 16 x $0.04).
- **T16 IS READY TO RUN AND NEEDS TWO OPERATOR DECISIONS FIRST (D53):**
  1. **B1** — confirm `FAL_KEY` was rotated. The first version of this
     session's brief confirmed it; the re-sent version deleted that line, so it
     is treated as unconfirmed. D36 is a disclosed-key incident and T16 spends.
  2. **The cap, and whether to buy the LoRA candidate.** $0.7125 with
     `fal_flux_general`, $0.4875 without — but without it nothing tests a clay
     LoRA. The amendment's $0.53 covers neither.
  Also needed: a **FLUX** claymation LoRA URL (not SDXL — the only endpoint
  with both ControlNet and LoRA is Flux-based).
  Instrumentation is in place: `--drift-sample` is stratified by chain depth,
  so the max_chain sweep is answerable from the same run.
- (superseded) T16 was previously described as follows:
  ~$0.95 total. BLOCKED on the operator confirming B1 and authorising spend.
  It is the first real datum for: both mode target vectors (D49 — the resynth
  gate REFUSES paid runs until it lands), the T18 chain-length default (D51),
  and every threshold F4/D48 says cannot be trusted from fixtures.
  When it runs, `claypipe canary restyle` (Track A, 3 frames) and
  `canary restyle --clip 5.1` (Track C — 5.06s is VACE's real floor per D50,
  not the 3s the plan assumed) are the commands. Print the full component
  vector per frame before batching.
- T17 duration invariant: replace frame-count equality with
  duration-within-one-frame plus the UNCHANGED audio MD5 gate. Decimate 16fps
  -> 12fps on the restyled panel (decimation, NOT interpolation — it would
  smooth out the stop-motion stepping D40 says is clay's signature). Code is
  local; needs a real Track C output to verify against, so it follows T16.
- T19 production backend (Modal + SD1.5 + ControlNet + clay LoRA). BLOCKED on a
  Modal account. **A CLAY LoRA is the highest-leverage item on the cost table**
  — it is what makes a $0.0023 SD1.5 call competitive with a $0.04 Kontext
  call for this look, because the style stops having to be carried by the
  prompt.
- **RUNBOOK.md and CONFIG.md (Rule 33) are STILL NOT WRITTEN.** This is now a
  larger gap than it was: the CLI grew `canary restyle`, `captions`,
  `--propagate`, `--clip`, `--single-shot`, `--mode` and three new config
  blocks this session. Overdue.
- **BLOCKERS, operator-owned (MASTER_PLAN §0):**
  - **B1** — confirm `FAL_KEY` was ROTATED, not reused. D36 records a key pasted
    into a session transcript. A new key is installed at `.env` this session,
    but only the operator knows whether it is new.
  - **B2** — REVOKE the GCP service-account key at
    `socialpilot-ui/config/googleAuth.json`, tracked in a PUBLIC repo (commit
    `59c9a14`) with Editor access to the sheet. Revoke FIRST; removing the file
    is not a fix. Blocks T24.
  - **B3** — decide which account owns the Railway service. The CLI is
    authenticated as `shoppykid1@gmail.com`, not `kyeboah@kymediamgmt.com`.
    Blocks T21/T22.
- **Dashboard workstream (D32), in dependency order:**
  1. `claypipe export` snapshot contract — DONE.
  2. Railway: API service + persistent store that accepts snapshots and serves
     them; returns canary verdicts. Needs `railway link` (no project yet).
  3. Vercel: Next.js dashboard reading that API.
  4. `claypipe publish` — push a snapshot from a local run; poll for a remote
     verdict so the canary gate can be cleared from the dashboard.
  Auth for the API is UNDECIDED and must be settled before step 2 ships — a
  world-readable endpoint would expose run metadata and spend.
- **ARM THE AUTO-CHECKPOINT HOOK (operator action, D26).** `.claude/settings.json`
  does not exist yet; creating it was blocked by the permission classifier. The
  hook script is committed at `.claude/hooks/checkpoint.sh` and verified working.
  To arm it, either run `/hooks` and add a Stop hook running
  `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/checkpoint.sh"`, or create
  `.claude/settings.json` by hand with that Stop hook entry. Until then, saving
  stays manual (which is what every step so far has done anyway).
- ~~A4 — Drive folder ID~~ CLOSED as out of scope by operator direction, see D8.
  Cross-repo integration deferred; ClayPipe's output dir is the contract surface.
- Step 2 — scoring module. DONE, green. D9 and D10 CLOSED by Decisions 1 and 2.
- Step 3 — retry + firewalls. DONE, green.
- Step 4 — human gates. DONE, green. Pages + loaders shipped, not yet wired.
- D15 open (TF flags real motion), D16 recorded, D18 open (targets_missed retry
  path — reseed or strength nudge?), D19 open (keep the project cap?). None of
  these blocks step 4.
- Step 5 — FalBackend. CLEARED TO BUILD. It also owns the wiring Step 4
  deliberately left undone: `poll_canary_verdict` before authorising any frame
  call, `merge_prompt_override` on an "adjust" verdict, `Scorer` +
  `RetryController` into `cli.batch` (D25), and an operator-facing submit
  command that feeds `parse_form_submission` (D22).
- D12 open: how should styles with no character (e.g. `logo`) score ID?
- Step 4 — human gate HTML pages; Step 5 — FalBackend; Step 6 — CLI polish + README.
- A2 — Flux Kontext vs SDXL+ControlNet stays deferred until after the first real
  canary. Do not pre-optimize.
- Captions (Whisper → SRT) are not built yet; `assemble` already burns in an SRT
  if one is present at `subs.srt`, so step-6 wiring is a drop-in.
- RUNBOOK.md / CONFIG.md (Rule 33) not written yet — due with step 6.

## Log (append-only, newest first)

### 2026-09-16 — v2v architecture: V1-V7 landed; the canary says no
- V1 frames_div_16 pricing (D59), V2 VaceBackend (D60), V3 propagation retired
  (D61), V4 metric vector with FLOW (D62), V5 shot-aligned chunking + V6 clip
  floor (D63), C4 caption inpainting (D66), V7 the paid canary (D64, D65).
- Commits: ec30116, f918c97, 2587f2e, 5571de0, c0ca0a6, cef1b30, 1c1d9b4,
  f9eb464. All pushed.
- **$0.6075 spent. V7 returned a clear negative.** Four defects found by
  RUNNING it, three of which cost or nearly cost money — see D64/D65.
- pytest 546 passed / 6 skipped / 0 failed.
- NOT done: V8 (blocked on the D64 decision). B2 and B3 untouched and still
  blocking the publish and dashboard tasks respectively.

### 2026-09-15 — T9a, T9b, T18a, RUNBOOK+CONFIG; T16 verified but UNSPENT
- **T9a** aspect-fit layout (D54). Commit `79142f0`. Square sources work.
- **T9b** burned-in text detection (D55). Commit `2cd77be`. Two wrong
  assumptions corrected by measurement; three bugs found while validating.
- **T18a** independent drift comparator (D56). Commit `a7a4d30`. Propagation
  had no drift check at all.
- **RUNBOOK.md + CONFIG.md + Rule 33 enforcement test** (D57). Commit
  `f8589c5`. The test caught six gaps in my own CONFIG.md immediately.
- **T16 prices verified against fal's live model pages** (D53) and applied.
  Found our own `fal` price under-stated at 0.035 vs the real 0.04, which would
  have made the affordability ceiling refuse every real call.
- **T16 NOT RUN. Stopped before spending, as instructed.** Two operator
  decisions outstanding — see Pending/Next.
- pytest 440 passed / 5 skipped / 0 failed. All commits pushed.
- VERIFIED not assumed: square layout checked on rendered pixels; burn-in
  detector calibrated against four clips; drift comparator proven not to touch
  the flow field; every doc claim enforced by a test; every fal price read off
  the vendor's own page.

### 2026-09-14 (cont.) — Phase 2 partial: T13, T14, T15, T18
- **T13+T15** per-mode target vectors, two backend protocols, unit-aware
  pricing (D49). Commit `0875983`. Also fixed Rule 33 drift: `status` still
  claimed captions were "not built yet".
- **T14** clip canary (D50). Commit `c372e25`. Corrected the plan's
  3-second/$0.12 VACE canary to its real 5.06s/$0.20 floor.
- **T18** adaptive keyframe propagation (D51). Commit `5009dd3`. 12.2x fewer
  paid frames measured; T18's TF>=0.99 criterion REFUTED as the wrong metric
  and reported as not met rather than as passed.
- **Caption overflow fix** (D52). Commit `fe16fe2`. Found by extracting a frame
  from the end-to-end render and looking at it.
- Full pipeline verified end to end on a real 60s clip — see Current State.
- pytest 372 passed / 3 skipped / 0 failed. All commits pushed to origin/main.
- NOT done: T16 (needs spend + B1), T17 (needs a Track C output), T19 (needs
  Modal), Phase 3 (T20-T22, needs B3), Phase 4 (T23-T24, T24 needs B2).
  RUNBOOK.md / CONFIG.md still unwritten.

### 2026-09-14 — MASTER_PLAN landed; Phase 1 (T9-T12) complete
- `MASTER_PLAN.md` written as the single build plan, superseding the three
  working-note briefs. Claymation target recorded (D40). All §1 measurements
  re-verified independently; three corrections found (D41). Commit `b1f7b39`.
- **T9** layout engine — panel height derived from source aspect, fixed
  geometry retired (D42). Commit `e50bc64`.
- **T11** shots.py — per-shot seeds, boundary-aware temporal scoring, D30 and
  D15/D34 closed (D45). Commit `a1698c5`. Taken before T10 because shot
  detection is what gives the canary its real most-motion frame, which T10 then
  locks as a reference.
- **T10** identity references from the approved canary (D43), plus T10a
  `canary restyle` (D44). Commit `10b3965`.
- **T12** captions as a layout element in the gap band (D46). Commit `d5270bf`.
- Two bugs found by running the flows, not by reading code (D47). The synthetic
  fixtures' inability to demonstrate F1 recorded as D48.
- VERIFIED, not assumed: geometry checked on rendered PIXELS (background-colour
  band boundaries, both aspects, summing to 1920); audio MD5 identical across
  extracted/uncaptioned/captioned; detector cut counts against the real clips;
  ID measured both ways on real footage; no-double-spend asserted across the
  canary and batch stages.
- pytest 299 passed / 3 skipped / 0 failed, up from 190 at day start.
- NOT done, and not started: Phase 2 (T13-T19), Phase 3 (T20-T22), Phase 4
  (T23-T24). Blockers B1/B2/B3 are operator-owned and untouched.

### 2026-09-12 — T4-T8: paid-backend locks, stubbed FalBackend, docs
- T4: two independent locks in front of any paid endpoint (--live for intent,
  FAL_KEY for capability), checked before anything touches disk. Added
  `claypipe/__main__.py` and a `--backend` override on batch.
- T5: FalBackend stubbed. No network code in the file; fal-client still not
  installed. The price in weights.yaml is a CEILING, not a guess.
- T6: `docs/REAL_RUN.md` — verbatim end-to-end transcript, including the
  batch that REFUSES for want of a canary verdict, because a happy-path-only
  doc would misrepresent the tool.
- T7: SKIPPED, no FAL_KEY (D36). No live call has ever been made.
- 190 green.

### 2026-09-12 — T1-T3: stale text, extract sentinel, D15 closed
- T1: cli.py claimed scoring was unbuilt three steps after it shipped; README
  listed four shipped steps as "not started"; SPEC.md gained the weights.yaml
  authority header. All three pinned by tests.
- T2: extraction resumability moved from a frame count to a `.extract_complete`
  sentinel carrying the source hash, so a partial extraction can no longer be
  mistaken for a finished one and a changed source cannot reuse stale frames.
- T3/D34: measured all three aggregations before choosing. `p95` INVERTS the
  motion/flicker ordering and would have made D15 worse; `block_p95` passes all
  three assertions and is now the default. D15 closed.
- 178 green.

### 2026-09-09 — Direction change mid-step: dashboard workstream opened
- Operator asked to "see track and interact with this tool" (D32). Step 5 was
  parked CLEAN at commits 1-5 — nothing half-finished, nothing unpushed.
- Built `claypipe/snapshot.py` + `claypipe export`: one JSON document per run
  (stage, progress, canary gate state, scores, cost, incidents, artefacts), and
  an index across runs. Path-scrubbed by construction (D33).
- Step 5 commits 6-7 (--live flag, FalBackend) remain buildable and unstarted.
  Commits 8-10 need a real FAL_KEY, which this machine does not have.
- 9 new tests, 166 green.

### 2026-09-09 — Step 5 commits 3-5: canary render / pack / submit
- `claypipe canary render|pack|submit` close the loop Step 4 left open: the
  pages existed but nothing generated them and nothing recorded the verdict.
- render also emits the FLAG page when the run has scores, so a reviewer sees
  what the scorer already flagged; it says so plainly when nothing is graded.
- Live demo: 77,512-byte canary page, three inlined frames, zero external refs;
  pack prints a file:// URL and starts no server; submit round-trips an
  "adjust" verdict with its prompt override and echoes per-frame decisions.
- 10 new tests, 157 green.

### 2026-09-09 — Step 5 commit 2: Scorer + RetryController wired (D25 closed)
- `restyle_frames` now threads a Scorer and a RetryController; every frame on a
  paid backend is scored, recorded to `scores.jsonl`, and retried per the policy.
- Two firewalls proved themselves against my own test scaffolding rather than in
  theory: the ledger REFUSED an unpriced stub backend, and the 15% retry budget
  HALTED a stub that never recovered (9 retries on 60 frames). Both are now
  regression tests.
- Calibration note: a perfect backend (restyled == source) with a reference drawn
  from the same clip scores SSIM 1.000 / LPIPS 0.000 / ID 1.000 / TF ~0.996 —
  comfortably PASS. A reference from an UNRELATED image fails identity on every
  frame and burns the retry budget, which is correct behaviour and worth knowing
  before the first real canary.
- 147 green.

### 2026-09-09 — Step 5 commit 1: canary gate wired into cli.batch
- `cli.batch` now consults `verdi.loaders` instead of `retry.require_canary_approval`.
  Three outcomes: true proceeds, false stops with the operator's reason verbatim,
  "adjust" records the revised prompt beside the run and STOPS (an adjusted
  prompt means the canary must be re-shot before a batch is worth paying for).
- `--wait-for-canary` opts into polling; default fails fast so a missing verdict
  is a refusal, not a ten-minute hang.
- The override prompt is now actually USED on the next run, and a dangling
  override pointer fails loudly rather than falling back to styles.yaml — a
  silent fallback would spend money on a prompt nobody approved.
- 7 new tests, 138 green.

### 2026-09-09 — Step 4 shipped: human gate pages + verdict loaders
- New package `claypipe/verdi/`: `canary_page.py`, `flag_page.py`, `loaders.py`,
  plus shared HTML primitives in `__init__.py`. `claypipe/review/` was never
  created, per the brief reserving that path.
- Pages are ONE file with ZERO external references — verified by walking every
  src/href/action in the rendered output, not just by intent. 21,895 bytes
  (3 inline PNGs) and 62,854 bytes (17 inline PNGs).
- Round-trip proven byte-identical: address-bar URL -> `canary_verdict_from_form`
  -> `canary_verdict.json` -> `read_canary_verdict` -> same dict, empty diff.
- Fixed a numbering defect inherited from the previous session: D13 had been
  used twice. The earlier entry is now D20 (see its note); the later D13 keeps
  its number because `weights.yaml` and `tests/test_score.py` already cite it.
- 32 new tests; 131 total green, 97 pass / 34 skip cold. Step 5 NOT started.

### 2026-09-09 — Step 3 shipped: retry policy + five cost firewalls
- Built `pipeline/retry.py`: `RetryController` (per-frame cap, whole-run budget,
  kill switch), `SpendLedger` (authorize-before-call, reconcile-after, run and
  project totals), `require_canary_approval()`, and incident notes for every
  breach. All numbers come from a new `firewalls:` block in `weights.yaml`.
- Wired the canary gate and the ledger into `cli.batch`; `--max-cost-usd` added.
- `restyle_frames()` now authorises every call to the ledger BEFORE executing it
  and reconciles the actual after. `reconcile()` requires the id that only
  `authorize()` returns, so an unledgered call is not expressible.
- 19 new tests in `tests/test_retry.py`, all five named acceptance tests green.
  One pre-existing e2e test failed on the new gate — the gate working correctly;
  updated to approve the canary first, mirroring the real Stage 1 -> Stage 2 flow.
- Retry actions branch on the verdict REASON (Decision 1), not the verdict alone.
- Total 99 tests green. Step 4 NOT started.

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

## Auto-checkpoint log (newest first)

Written by `.claude/hooks/checkpoint.sh` on the Stop hook. These are safety-net
commits, not the deliberate step checkpoints P2 asks for — those are the entries
in the Log section above.

<!-- checkpoint-insert -->
- 2026-09-09T23:15:41Z — 1 file(s): .claude/ 
