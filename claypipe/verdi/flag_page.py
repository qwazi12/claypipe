"""STAGE 3 — the flag review page (SPEC §5).

Every frame the scorer did not auto-accept, PLUS a deterministic random audit
sample of frames it did. The audit sample is the important half: without it a
quietly mis-calibrated scorer produces a page of nothing-to-see-here and gets
rubber-stamped.

Output contract — `runs/<run_id>/review_verdicts.json`:
    schema_version, decided_at,
    frames: {name: {category, verdict: accept|retry|reject, note, score_summary}}

A `reject` never silently falls back to the source frame. It writes the frame to
`rejected_frames/` with a per-frame `.rejection.json` note and increments
`qc_card.human_overrides`, so a rejection is visible in the record rather than
being an absence.
"""

from __future__ import annotations

import json
import random
import shutil
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from ..config import REPO_ROOT, ReviewConfig
from ..logging import utc_now
from ..pipeline.retry import write_incident
from ..pipeline.score import FrameScore
from . import FIELD_SEP, data_uri, esc, find_decisions, page_shell
from .canary_page import UNSCORED_CAVEAT_DECISIONS

PAGE_NAME = "flag_review.html"
VERDICT_NAME = "review_verdicts.json"
REJECTED_DIR = "rejected_frames"
HIGH_OUTLIER_FINDING = "high_outlier_rate"

# The human picks from these three. HUMAN_QUEUE is never offered: it is set by
# the budget logic when retries run out, not chosen by a reviewer.
FRAME_CHOICES = ("accept", "retry", "reject")

CATEGORY_OUTLIER = "outlier_audit"
CATEGORY_AUDIT = "random_audit"


@dataclass(frozen=True)
class ReviewCard:
    name: str
    image: Path
    category: str
    score: FrameScore | None = None

    def score_summary(self) -> dict:
        if self.score is None:
            return {"f": None, "reason": None, "missed": []}
        return {
            "f": round(self.score.f, 4),
            "reason": self.score.reason.value,
            "missed": self.score.missed_targets,
        }


@dataclass(frozen=True)
class Selection:
    """What the page will show, and what it had to leave out."""

    cards: list[ReviewCard]
    outlier_total: int
    audit_total: int
    truncated: bool

    @property
    def shown(self) -> int:
        return len(self.cards)


def select_review_frames(
    scores: list[FrameScore],
    images: dict[str, Path],
    cfg: ReviewConfig,
    *,
    seed: int | None = None,
) -> Selection:
    """Outliers, plus a deterministic random audit sample of the passers.

    Determinism is a review property, not a nicety: the same run must produce
    the same audit set every time, so a second reviewer sees what the first saw
    and a disputed review can be reproduced.
    """
    outliers = [s for s in scores if s.f <= cfg.outlier_f_threshold]
    passers = [s for s in scores if s.f > cfg.outlier_f_threshold]

    sample_size = int(len(passers) * cfg.audit_sample_fraction)
    rng = random.Random(cfg.audit_sample_seed if seed is None else seed)
    # Sample from a name-sorted list so the draw cannot depend on input order.
    audit = rng.sample(sorted(passers, key=lambda s: s.frame), sample_size) if sample_size else []

    outliers.sort(key=lambda s: (s.f, s.frame))
    truncated = len(outliers) > cfg.max_outlier_cards
    kept = outliers[: cfg.max_outlier_cards]

    cards = [
        ReviewCard(name=s.frame, image=images[s.frame], category=CATEGORY_OUTLIER, score=s)
        for s in kept
        if s.frame in images
    ] + [
        ReviewCard(name=s.frame, image=images[s.frame], category=CATEGORY_AUDIT, score=s)
        for s in sorted(audit, key=lambda s: s.frame)
        if s.frame in images
    ]
    return Selection(
        cards=cards,
        outlier_total=len(outliers),
        audit_total=len(audit),
        truncated=truncated,
    )


def _card_html(card: ReviewCard) -> str:
    verdict = card.score.verdict.value if card.score is not None else "UNSCORED"
    field = f"frame{FIELD_SEP}{card.name}"
    choices = ""
    for choice in FRAME_CHOICES:
        checked = " checked" if choice == "accept" else ""
        choices += (
            f'<label><input type="radio" name="{esc(field)}{FIELD_SEP}verdict" '
            f'value="{choice}"{checked}> {choice}</label>'
        )
    summary = card.score_summary()
    missed = ", ".join(summary["missed"]) or "none"
    f_text = f"{summary['f']:.3f}" if summary["f"] is not None else "not scored"
    return (
        '<article class="card">'
        f'<img src="{data_uri(card.image)}" alt="restyled frame {esc(card.name)}">'
        '<div class="body">'
        f"<h3>{esc(card.name)} <span class=\"pill {esc(verdict)}\">{esc(verdict)}</span></h3>"
        '<table class="metrics">'
        f"<tr><td>category</td><td>{esc(card.category)}</td></tr>"
        f"<tr><td>F</td><td>{esc(f_text)}</td></tr>"
        f"<tr><td>reason</td><td>{esc(summary['reason'] or '-')}</td></tr>"
        f"<tr><td>missed targets</td><td"
        f"{' class=\"miss\"' if summary['missed'] else ''}>{esc(missed)}</td></tr>"
        "</table>"
        f'<input type="hidden" name="{esc(field)}{FIELD_SEP}category" '
        f'value="{esc(card.category)}">'
        f'<input type="hidden" name="{esc(field)}{FIELD_SEP}f" value="{esc(summary["f"])}">'
        f'<input type="hidden" name="{esc(field)}{FIELD_SEP}reason" '
        f'value="{esc(summary["reason"] or "")}">'
        f'<input type="hidden" name="{esc(field)}{FIELD_SEP}missed" '
        f'value="{esc(",".join(summary["missed"]))}">'
        f"<fieldset>{choices}</fieldset>"
        f'<input type="text" name="{esc(field)}{FIELD_SEP}note" placeholder="note (optional)">'
        "</div></article>"
    )


def render_flag_page(
    *,
    run_id: str,
    backend: str,
    selection: Selection,
    cfg: ReviewConfig,
    memory_path: Path | None = None,
) -> str:
    scored = all(c.score is not None for c in selection.cards) and bool(selection.cards)
    caveats = []
    if not scored or backend == "dummy":
        path = memory_path or (REPO_ROOT / "memory.md")
        if path.is_file():
            caveats = find_decisions(path.read_text(), UNSCORED_CAVEAT_DECISIONS)

    banner = ""
    if selection.truncated:
        banner = (
            '<section class="caveat"><h2>Truncated</h2>'
            f"<p><strong>{selection.outlier_total} frames were flagged; this page shows "
            f"the worst {cfg.max_outlier_cards}.</strong></p>"
            "<p>A run flagging this many frames has a systemic problem that "
            "reviewing frames one at a time will not fix. An incident note with "
            "the true count has been written to the run's "
            "<code>incidents/</code> directory.</p></section>"
        )

    body = (
        banner
        + f'<form action="{esc(PAGE_NAME)}" method="get">'
        '<input type="hidden" name="schema_version" value="1">'
        f'<input type="hidden" name="run_id" value="{esc(run_id)}">'
        '<section class="panel"><h2>Flagged frames</h2>'
        f"<p class=\"hint\">{selection.outlier_total} outliers "
        f"(showing {min(selection.outlier_total, cfg.max_outlier_cards)}) and "
        f"{selection.audit_total} random audit frames drawn deterministically at "
        f"{cfg.audit_sample_fraction:.0%} of the passing frames.</p>"
        '<button type="submit">Submit review</button>'
        '<p class="hint">Submitting puts every decision in your address bar. '
        "Copy that URL to the CLI; nothing is sent anywhere.</p></section>"
        f'<div class="cards">{"".join(_card_html(c) for c in selection.cards)}</div>'
        "</form>"
    )
    return page_shell(
        title="ClayPipe flag review",
        subtitle=f"run {run_id} · backend {backend} · {selection.shown} cards",
        caveats=caveats,
        body=body,
    )


def write_flag_page(
    run_dir: Path,
    *,
    run_id: str,
    backend: str,
    selection: Selection,
    cfg: ReviewConfig,
    memory_path: Path | None = None,
    open_browser: bool = False,
) -> Path:
    """Render the page, and surface a finding if the flag rate was excessive."""
    if selection.truncated:
        from ..run import RunPaths

        write_incident(
            RunPaths(run_dir),
            HIGH_OUTLIER_FINDING,
            {
                "outliers_total": selection.outlier_total,
                "cards_shown": cfg.max_outlier_cards,
                "dropped": selection.outlier_total - cfg.max_outlier_cards,
                "audit_sample": selection.audit_total,
                "note": (
                    "Flagged-frame count exceeded the reviewable cap. The page "
                    "shows the worst frames only; this note records the true "
                    "count so the excess is surfaced rather than dropped."
                ),
            },
        )
    html_text = render_flag_page(
        run_id=run_id, backend=backend, selection=selection, cfg=cfg,
        memory_path=memory_path,
    )
    dst = run_dir / PAGE_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(html_text)
    if open_browser:  # pragma: no cover - operator convenience
        webbrowser.open(dst.resolve().as_uri())
    return dst


def apply_rejections(
    run_dir: Path, verdicts: dict, restyled_dir: Path
) -> list[Path]:
    """Move every rejected frame aside and write a note saying why.

    A rejection must never look like an absence: the frame is preserved under
    `rejected_frames/` and paired with a `.rejection.json` carrying the
    category, the note and the score summary that justified it.
    """
    rejected_root = run_dir / REJECTED_DIR
    written: list[Path] = []
    for name, entry in sorted(verdicts.get("frames", {}).items()):
        if entry.get("verdict") != "reject":
            continue
        rejected_root.mkdir(parents=True, exist_ok=True)
        source = restyled_dir / name
        if source.is_file():
            shutil.copy2(source, rejected_root / name)
        note_path = rejected_root / f"{name}.rejection.json"
        note_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frame": name,
                    "rejected_at": utc_now(),
                    "category": entry.get("category"),
                    "note": entry.get("note", ""),
                    "score_summary": entry.get("score_summary", {}),
                },
                indent=2,
            )
            + "\n"
        )
        written.append(note_path)
    return written


def count_human_overrides(verdicts: dict) -> int:
    """`qc_card.human_overrides` — decisions where a human contradicted the scorer."""
    return sum(
        1
        for entry in verdicts.get("frames", {}).values()
        if entry.get("verdict") in ("reject", "retry")
    )
