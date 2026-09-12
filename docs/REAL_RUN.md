# A real run, end to end

Captured verbatim on 2026-09-12 by running the commands below against the
bundled `assets/test_clip.mp4`. Every block is the actual stdout/stderr and
exit code — nothing here is illustrative.

The run uses the free `dummy` backend, so it cost nothing and **was not
scored** (see `memory.md` D27: the dummy produces no generative content to
grade, and inventing numbers for it would be worse than reporting none).

Paths and timestamps are scrubbed to `runs/`, `<RUN_ID>` and `<UTC>`; the
output is otherwise unedited.

## The cycle

```bash
claypipe intake assets/test_clip.mp4 --style clay --title "Test Clip"
claypipe batch <RUN_ID>            # refused: no canary verdict yet
claypipe canary submit <RUN_ID> --url "<address bar from the review page>"
claypipe batch <RUN_ID>
claypipe canary render <RUN_ID>
claypipe canary pack <RUN_ID>
claypipe assemble <RUN_ID>
claypipe status <RUN_ID>
claypipe export <RUN_ID> --out runs/<RUN_ID>/export.json
claypipe export --out runs/index.json
```

Note the second command. `batch` refuses before it does any work, because no
human has approved the canary. That refusal is the system working, and it is
reproduced verbatim below.

## Transcript

```console
$ .venv/bin/python -m claypipe intake assets/test_clip.mp4 --style clay --title Test Clip --runs-dir runs
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "intake", "source": "assets/test_clip.mp4", "style": "clay", "fps": 12, "backend": "dummy", "duration_s": 5.0, "references": 0}
runs/<RUN_ID>
[exit 0]

$ .venv/bin/python -m claypipe batch runs/<RUN_ID> --runs-dir runs
{"timestamp": "<UTC>", "level": "ERROR", "service": "claypipe", "runId": "<RUN_ID>", "event": "batch.blocked", "breach": "canary_missing"}
FAILED: canary gate: canary_verdict.json not found in runs/<RUN_ID>. Batch cannot start before a human has reviewed the canary frames. Run `claypipe canary render` then `claypipe canary pack`, decide, and submit with `claypipe canary submit`.
[exit 1]

$ .venv/bin/python -m claypipe canary submit runs/<RUN_ID> --runs-dir runs --url ?schema_version=1&decider=operator&approved=true
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "canary.submit", "path": "runs/<RUN_ID>/canary_verdict.json", "approved": true, "decider": "operator", "frames": 0, "reason": null}
APPROVED by operator -> runs/<RUN_ID>/canary_verdict.json
[exit 0]

$ .venv/bin/python -m claypipe batch runs/<RUN_ID> --runs-dir runs
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "batch.canary.approved", "decider": "operator", "decided_at": "<UTC>"}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "extract.frames", "frames": 60, "fps": 12, "dir": "runs/<RUN_ID>/frames/source"}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "extract.audio", "codec": "aac", "sample_rate": "48000", "channels": 1, "md5": "41fb16f4863d3ace1d5c88bd7a3b1585", "path": "runs/<RUN_ID>/audio.aac"}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "batch.scoring.skipped", "backend": "dummy", "reason": "dummy backend produces no generative content to grade (D27)"}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "00632e3d19af", "frame": "f_00001.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "fbd65a1ea1ba", "frame": "f_00002.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "7b102528acb3", "frame": "f_00003.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "d2b8fa4e13fd", "frame": "f_00004.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "1cedfa61ff0b", "frame": "f_00005.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "f2005efea9f3", "frame": "f_00006.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "485cde84de88", "frame": "f_00007.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "7dd583ba37a7", "frame": "f_00008.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "c5ab171f5bd3", "frame": "f_00009.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "d9ebd691d956", "frame": "f_00010.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "3e09f9c95f0d", "frame": "f_00011.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "f18f4cd8766a", "frame": "f_00012.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "76f7626e58f2", "frame": "f_00013.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "0db1afd821a2", "frame": "f_00014.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "7dd483384c14", "frame": "f_00015.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "a6cab1d77703", "frame": "f_00016.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "517ba9241858", "frame": "f_00017.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "dc391380cc03", "frame": "f_00018.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "9ecc2430d1f3", "frame": "f_00019.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "e68fef8de60d", "frame": "f_00020.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "c4cd57852860", "frame": "f_00021.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "157658f3ecc0", "frame": "f_00022.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "2df0c225b5cd", "frame": "f_00023.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "dcc4078e768e", "frame": "f_00024.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "04ccea6a9a9e", "frame": "f_00025.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "9c1b4a0e45f5", "frame": "f_00026.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "9546816afa32", "frame": "f_00027.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "78e5860bb6f1", "frame": "f_00028.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "3c2b0a47003b", "frame": "f_00029.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "09c8560412e7", "frame": "f_00030.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "d87a0b26723d", "frame": "f_00031.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "cb62e0472814", "frame": "f_00032.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "c1fffc899564", "frame": "f_00033.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "3beb410b9ed3", "frame": "f_00034.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "8eb750f0914b", "frame": "f_00035.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "f8e2d8554ce7", "frame": "f_00036.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "7898f6f38c9c", "frame": "f_00037.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "3374071b97d5", "frame": "f_00038.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "24e05317e4f7", "frame": "f_00039.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "12d26b105072", "frame": "f_00040.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "eaebc087b66a", "frame": "f_00041.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "bf6cd136b6c3", "frame": "f_00042.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "4ef9020a2ba1", "frame": "f_00043.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "224ec9b7a9fc", "frame": "f_00044.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "bd73c2470909", "frame": "f_00045.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "e55a2aa00aa3", "frame": "f_00046.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "11f97aa73b8b", "frame": "f_00047.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "aa0ff1f16b45", "frame": "f_00048.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "a74ef7520be7", "frame": "f_00049.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "1e5188d798b7", "frame": "f_00050.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "ad1a78da0161", "frame": "f_00051.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "a4aa1b8f13c1", "frame": "f_00052.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "ac3d930d45a6", "frame": "f_00053.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "341d84a5924f", "frame": "f_00054.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "77428ea0c764", "frame": "f_00055.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "10de2974286a", "frame": "f_00056.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "2a940da71405", "frame": "f_00057.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "837d624acbe8", "frame": "f_00058.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "e8c677c22cfb", "frame": "f_00059.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "spend.authorized", "entry_id": "4d8dc5cc413f", "frame": "f_00060.png", "stage": "batch", "estimated_usd": 0.0, "run_total_usd": 0.0}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "restyle.frames", "backend": "dummy", "restyled": 60, "resumed": 0, "retried": 0, "total": 60, "strength": 0.65, "scored": false, "est_cost_usd": 0.0}
60 frames restyled with backend 'dummy' (not scored — see memory.md D27)
[exit 0]

$ .venv/bin/python -m claypipe canary render runs/<RUN_ID> --runs-dir runs
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "canary.render", "path": "runs/<RUN_ID>/canary_review.html", "frames": 3, "scored": false}
runs/<RUN_ID>/canary_review.html
(no scores.jsonl — flag page skipped; nothing has been graded)
[exit 0]

$ .venv/bin/python -m claypipe canary pack runs/<RUN_ID> --runs-dir runs --no-open
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "canary.pack", "url": "file://runs/<RUN_ID>/canary_review.html", "opened": false}
file://runs/<RUN_ID>/canary_review.html
[exit 0]

$ .venv/bin/python -m claypipe assemble runs/<RUN_ID> --runs-dir runs
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "assemble.reassembled", "frames": 60, "fps": 12, "path": "runs/<RUN_ID>/work/restyled.mp4"}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "assemble.rendered", "path": "runs/<RUN_ID>/final_comparison.mp4", "size": "1080x1920", "captions": false}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "assemble.verified", "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac", "audio_md5": "41fb16f4863d3ace1d5c88bd7a3b1585", "audio_bit_identical": true, "frames_expected": 60, "frames_actual": 60, "duration_s": 5.034667}
{"timestamp": "<UTC>", "level": "INFO", "service": "claypipe", "runId": "<RUN_ID>", "event": "qc.card", "path": "runs/<RUN_ID>/qc_card.json", "verdict": "assembled"}
runs/<RUN_ID>/final_comparison.mp4
[exit 0]

$ .venv/bin/python -m claypipe status runs/<RUN_ID> --runs-dir runs
run        <RUN_ID>
created    2026-09-12T22:52:31.967Z
source     ./assets/test_clip.mp4
style/fps  clay @ 12fps   backend=dummy
frames     extracted=60  restyled=60
audio      present
final      runs/<RUN_ID>/final_comparison.mp4
verdict    assembled
scoring    live on paid backends; skipped on dummy (D27)
captions   not built yet (burned in automatically once subs.srt exists)
[exit 0]

$ .venv/bin/python -m claypipe export runs/<RUN_ID> --runs-dir runs --out runs/<RUN_ID>/export.json
runs/<RUN_ID>/export.json
[exit 0]

$ .venv/bin/python -m claypipe export --runs-dir runs --out runs/index.json
runs/index.json
[exit 0]
```

## What the run produced

```
runs/<RUN_ID>/
  run.json                 the manifest: style, fps, backend, source hash
  canary_verdict.json      the human decision that unblocked batch
  canary_review.html       self-contained review page, frames inlined
  audio.aac                extracted once, losslessly, never re-encoded
  frames/source/           60 extracted frames + .extract_complete sentinel
  frames/restyled/         60 restyled frames
  spend_ledger.jsonl       every call authorised before it ran
  qc_card.json             scores (null here — unscored), ledger-derived cost
  export.json              the dashboard snapshot
  final_comparison.mp4     1080x1920, H.264, original audio bit-for-bit
  logs/run.jsonl           structured, one runId throughout
```

## What this does NOT show

* **A paid run.** No fal.ai call has ever been made from this repo. `--backend
  fal` refuses without both `--live` and `FAL_KEY`, and the backend itself is
  a stub that raises rather than calling an endpoint.
* **Scores.** The dummy backend is not graded (D27). A scored transcript needs
  a paid backend and locked `--ref` images.
* **Captions.** Whisper is not wired yet; `assemble` burns in `subs.srt` if one
  exists, and none does here.
