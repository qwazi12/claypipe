"""STAGE 1 — the canary review page (SPEC §4.1, §5).

Three restyled frames, their scores, and a GO / ADJUST / NO-GO decision. This
page is the only thing standing between a bad style prompt and a full batch of
paid calls, so it is built to be readable by a human who has not been staring
at the pipeline all day: every metric is spelled out, every missed target is
marked, and an unscored run says so in a warning block rather than showing a
reassuring blank.

Output contract — `runs/<run_id>/canary_verdict.json`:
    schema_version, decided_at, decider, approved (true|false|"adjust"),
    reason (required iff approved is false),
    prompt_override (required iff approved == "adjust"),
    frames: {name: {verdict: approve|adjust|reject, note: str}}
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass
from pathlib import Path

from ..config import REPO_ROOT
from ..pipeline.score import FrameScore
from . import FIELD_SEP, Caveat, data_uri, esc, find_decisions, page_shell

PAGE_NAME = "canary_review.html"
VERDICT_NAME = "canary_verdict.json"

FRAME_CHOICES = ("approve", "adjust", "reject")
RUN_CHOICES = (
    ("true", "APPROVE — clear the gate and let batch spend"),
    ("adjust", "ADJUST — re-run intake with a revised prompt first"),
    ("false", "REJECT — block the run"),
)

# Quoted verbatim on any page whose run has not actually been scored.
UNSCORED_CAVEAT_DECISIONS = ("D25",)


@dataclass(frozen=True)
class CanaryCard:
    """One canary frame: the picture, and what the scorer made of it."""

    name: str
    image: Path
    score: FrameScore | None = None
    caption: str = ""


def _metrics_table(score: FrameScore | None) -> str:
    if score is None:
        return (
            '<table class="metrics"><tr><td>scored</td>'
            '<td class="miss">no &mdash; see caveat above</td></tr></table>'
        )
    rows = [
        ("SSIM", score.ssim, "ssim"),
        ("LPIPS (edges)", score.lpips_edges, "lpips_edges"),
        ("Identity", score.identity, "id"),
        ("Temporal", score.temporal, "tf"),
    ]
    cells = ""
    for label, value, key in rows:
        missed = not score.targets_met.get(key, True)
        klass = ' class="miss"' if missed else ""
        flag = " &#9888; target missed" if missed else ""
        cells += f"<tr><td>{esc(label)}</td><td{klass}>{value:.3f}{flag}</td></tr>"
    cells += f"<tr><td><strong>F</strong></td><td><strong>{score.f:.3f}</strong></td></tr>"
    cells += f"<tr><td>reason</td><td>{esc(score.reason.value)}</td></tr>"
    return f'<table class="metrics">{cells}</table>'


def _card_html(card: CanaryCard) -> str:
    verdict = card.score.verdict.value if card.score is not None else "UNSCORED"
    field = f"frame{FIELD_SEP}{card.name}"
    choices = ""
    for choice in FRAME_CHOICES:
        checked = " checked" if choice == "approve" else ""
        choices += (
            f'<label><input type="radio" name="{esc(field)}{FIELD_SEP}verdict" '
            f'value="{choice}"{checked}> {choice}</label>'
        )
    return (
        '<article class="card">'
        f'<img src="{data_uri(card.image)}" alt="restyled frame {esc(card.name)}">'
        '<div class="body">'
        f"<h3>{esc(card.name)} <span class=\"pill {esc(verdict)}\">{esc(verdict)}</span></h3>"
        + (f'<p class="hint">{esc(card.caption)}</p>' if card.caption else "")
        + _metrics_table(card.score)
        + f"<fieldset>{choices}</fieldset>"
        f'<input type="text" name="{esc(field)}{FIELD_SEP}note" placeholder="note (optional)">'
        "</div></article>"
    )


def build_caveats(
    *, backend: str, scored: bool, memory_path: Path | None = None
) -> list[Caveat]:
    """Warn, in the operator's face, when a page cannot mean what it looks like.

    A `dummy`-backend run or a run with no scores would otherwise render a wall
    of clean-looking cards. That page must not be mistakable for a real result.
    """
    if scored and backend != "dummy":
        return []
    path = memory_path or (REPO_ROOT / "memory.md")
    caveats = (
        find_decisions(path.read_text(), UNSCORED_CAVEAT_DECISIONS)
        if path.is_file()
        else []
    )
    if not caveats:
        caveats = [
            Caveat(
                number="D25",
                headline="Scoring is not yet wired into batch",
                body="Scoring not yet wired into batch — see D25 in memory.md.",
            )
        ]
    return caveats


def render_canary_page(
    *,
    run_id: str,
    style: str,
    backend: str,
    cards: list[CanaryCard],
    prompt: str,
    memory_path: Path | None = None,
) -> str:
    """The whole page, as one self-contained string."""
    scored = bool(cards) and all(card.score is not None for card in cards)
    caveats = build_caveats(backend=backend, scored=scored, memory_path=memory_path)

    run_choices = ""
    for value, label in RUN_CHOICES:
        run_choices += (
            f'<label><input type="radio" name="approved" value="{value}"> {esc(label)}</label>'
        )

    decision_panel = (
        '<section class="panel"><h2>Decision</h2>'
        '<div class="row"><label for="decider">Decided by</label>'
        '<input type="text" id="decider" name="decider" value="operator"></div>'
        f"<div class=\"row\"><label>Verdict</label><fieldset>{run_choices}</fieldset></div>"
        '<div class="row"><label for="reason">Reason '
        "(required if you REJECT)</label>"
        '<textarea id="reason" name="reason"></textarea></div>'
        '<div class="row"><label for="prompt_override">Revised prompt '
        "(required if you ADJUST)</label>"
        f'<textarea id="prompt_override" name="prompt_override">{esc(prompt)}</textarea></div>'
        '<button type="submit">Submit decision</button>'
        '<p class="hint">Submitting puts the whole decision in your address bar. '
        "Copy that URL and hand it to the CLI &mdash; there is no server here, "
        "and nothing is sent anywhere.</p>"
        "</section>"
    )

    body = (
        f'<form action="{esc(PAGE_NAME)}" method="get">'
        f'<input type="hidden" name="schema_version" value="1">'
        f'<input type="hidden" name="run_id" value="{esc(run_id)}">'
        f"{decision_panel}"
        f'<div class="cards">{"".join(_card_html(c) for c in cards)}</div>'
        "</form>"
    )
    return page_shell(
        title="ClayPipe canary review",
        subtitle=f"run {run_id} · style {style} · backend {backend} · {len(cards)} frames",
        caveats=caveats,
        body=body,
    )


def write_canary_page(
    run_dir: Path,
    *,
    run_id: str,
    style: str,
    backend: str,
    cards: list[CanaryCard],
    prompt: str,
    memory_path: Path | None = None,
    open_browser: bool = False,
) -> Path:
    """Render to `runs/<id>/canary_review.html`, optionally opening it."""
    html_text = render_canary_page(
        run_id=run_id, style=style, backend=backend, cards=cards,
        prompt=prompt, memory_path=memory_path,
    )
    dst = run_dir / PAGE_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(html_text)
    if open_browser:  # pragma: no cover - operator convenience, not test surface
        webbrowser.open(dst.resolve().as_uri())
    return dst
