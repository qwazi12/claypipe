"""Canary review page tests (Build Order step 4).

The page is the last thing between a bad prompt and a batch of paid calls, so
these tests are about trustworthiness as much as correctness: it must work with
no network, no JavaScript and no server, and it must never look reassuring
about a run that was never scored.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from claypipe.config import ConfigError, load_weights
from claypipe.pipeline.score import FrameScore, Verdict, VerdictReason
from claypipe.verdi import canary_page as CP
from claypipe.verdi import loaders as L

FIXTURES = Path(__file__).parent / "fixtures"

# Anything that would make the page reach off the machine. Named explicitly so a
# failure says which class of offender crept in.
NETWORK_OFFENDERS = (
    "http://", "https://", "//cdn", "@import", "blob:", "file://",
    "fonts.googleapis", "cdnjs", "unpkg", "jsdelivr", "integrity=",
)


def make_score(name: str, *, f: float = 0.93, missed: str | None = None) -> FrameScore:
    targets = {"ssim": True, "lpips_edges": True, "id": True, "tf": True}
    if missed:
        targets[missed] = False
    return FrameScore(
        frame=name, ssim=0.91, lpips_edges=0.07, identity=0.94, temporal=0.99, f=f,
        verdict=Verdict.BORDERLINE if missed else Verdict.PASS,
        reason=VerdictReason.TARGETS_MISSED if missed else VerdictReason.ACCEPTED,
        targets_met=targets,
    )


@pytest.fixture
def frame_images() -> list[Path]:
    """Three real PNGs from the scoring fixtures."""
    return [
        FIXTURES / "identical" / "source.png",
        FIXTURES / "recolored_only" / "restyled.png",
        FIXTURES / "neumorphic" / "restyled.png",
    ]


@pytest.fixture
def cards(frame_images) -> list[CP.CanaryCard]:
    names = ["f_00001.png", "f_00030.png", "f_00060.png"]
    return [
        CP.CanaryCard(name=n, image=p, score=make_score(n), caption="canary frame")
        for n, p in zip(names, frame_images)
    ]


def render(cards, backend="fal", memory_path=None) -> str:
    return CP.render_canary_page(
        run_id="demo-run", style="clay", backend=backend, cards=cards,
        prompt="claymation, plasticine clay stop-motion",
        memory_path=memory_path,
    )


def test_canary_page_is_self_contained_no_network_calls(cards) -> None:
    """HARD: the page must not reference anything off the machine.

    Walks every src/href/@import/url() reference and rejects anything that is
    not a `data:` URI, a `#` fragment, or the page's own filename.
    """
    page = render(cards)

    for offender in NETWORK_OFFENDERS:
        assert offender not in page, f"page references {offender!r}"

    refs = re.findall(r'(?:src|href|action)\s*=\s*"([^"]*)"', page)
    assert refs, "expected at least the image and form references"
    for ref in refs:
        assert (
            ref.startswith("data:")
            or ref.startswith("#")
            or ref == CP.PAGE_NAME
        ), f"non-local reference: {ref[:60]}"

    assert "url(" not in page, "CSS url() could pull an external asset"
    assert "<link" not in page and "<script" not in page, "no external assets at all"


def test_canary_page_inlines_frames_as_data_urls(cards, frame_images) -> None:
    """Each frame is embedded as a decodable base64 PNG of the right size."""
    page = render(cards)
    payloads = re.findall(r'data:image/png;base64,([A-Za-z0-9+/=]+)', page)
    assert len(payloads) == 3

    for payload, source in zip(payloads, frame_images):
        decoded = base64.b64decode(payload)
        expected = source.read_bytes()
        assert abs(len(decoded) - len(expected)) <= 2
        assert decoded == expected
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n", "not actually a PNG"


def test_canary_page_form_encodes_verdict_payload() -> None:
    """A submitted form body decodes to the contract shape, all three states."""
    body = (
        "schema_version=1&run_id=demo-run&decider=kwasi&approved=true&reason=&"
        "prompt_override=&"
        "frame:f_00001.png:verdict=approve&frame:f_00001.png:note=clean&"
        "frame:f_00060.png:verdict=reject&frame:f_00060.png:note=melted"
    )
    parsed = L.canary_verdict_from_form(body)

    assert parsed["schema_version"] == 1
    assert parsed["approved"] is True
    assert parsed["decider"] == "kwasi"
    assert parsed["frames"]["f_00001.png"] == {"verdict": "approve", "note": "clean"}
    assert parsed["frames"]["f_00060.png"]["verdict"] == "reject"
    assert parsed["decided_at"].endswith("Z")

    # false requires a reason...
    with pytest.raises(ConfigError, match="reason"):
        L.canary_verdict_from_form("approved=false&reason=")
    rejected = L.canary_verdict_from_form("approved=false&reason=prompt+is+too+literal")
    assert rejected["approved"] is False
    assert rejected["reason"] == "prompt is too literal"

    # ...and 'adjust' requires a prompt override.
    with pytest.raises(ConfigError, match="prompt_override"):
        L.canary_verdict_from_form("approved=adjust&prompt_override=")
    adjusted = L.canary_verdict_from_form("approved=adjust&prompt_override=more+fingerprints")
    assert adjusted["approved"] == "adjust"
    assert adjusted["prompt_override"] == "more fingerprints"


def test_canary_page_accepts_a_full_url_from_the_address_bar() -> None:
    """What the operator actually has is the address bar, not a bare query."""
    url = (
        "file:///Users/x/runs/demo/canary_review.html?"
        "approved=true&decider=operator&frame:f_00001.png:verdict=approve"
    )
    parsed = L.canary_verdict_from_form(url)
    assert parsed["approved"] is True
    assert parsed["frames"]["f_00001.png"]["verdict"] == "approve"


def test_canary_page_adapt_state_documents_dummy_backend_caveat(cards, tmp_path) -> None:
    """A dummy-backend or unscored run must SAY it cannot mean what it shows.

    The caveat quotes a real D-number parsed out of memory.md, so the warning
    points at the audit trail rather than being loose prose.
    """
    unscored = [CP.CanaryCard(name=c.name, image=c.image, score=None) for c in cards]
    page = render(unscored, backend="dummy")

    assert "Caveat" in page
    quoted = re.findall(r"<code>(D\d+)</code>", page)
    assert quoted, "caveat must quote a D-number"

    memory = Path("memory.md").read_text()
    for number in set(quoted):
        assert f"**{number} " in memory or f"### {number}" in memory, (
            f"{number} is quoted on the page but absent from memory.md"
        )

    assert "no &mdash; see caveat above" in page, "unscored cards must say so"

    # A fully scored run on a real backend carries no caveat.
    clean = render(cards, backend="fal")
    assert "Caveat" not in clean


def test_canary_page_caveat_survives_a_missing_memory_file(cards, tmp_path) -> None:
    """The warning matters more than its citation: never drop it silently."""
    page = render(cards, backend="dummy", memory_path=tmp_path / "absent.md")
    assert "Caveat" in page and "D25" in page


def test_canary_page_never_auto_submits(cards) -> None:
    """HARD: submission is always an explicit click."""
    page = render(cards)
    assert page.count("<button type=\"submit\">") == 1
    for banned in ("onload", "onchange", "submit()", "autofocus", "http-equiv=\"refresh\""):
        assert banned not in page, f"page contains {banned!r}"


def test_canary_page_encodes_decisions_in_form_names_not_javascript(cards) -> None:
    """Persistence must not depend on JS: the names carry the whole decision."""
    page = render(cards)
    assert "<script" not in page
    for card in cards:
        assert f'name="frame:{card.name}:verdict"' in page
        assert f'name="frame:{card.name}:note"' in page
    assert 'name="approved"' in page and 'name="decider"' in page
    assert 'method="get"' in page, "GET is what puts the payload in the address bar"


def test_canary_page_marks_missed_targets_visibly(frame_images) -> None:
    """A missed component target must be legible, not buried in a number."""
    card = CP.CanaryCard(
        name="f_00007.png", image=frame_images[0], score=make_score("f_00007.png", f=0.81, missed="id")
    )
    page = render([card])
    assert "target missed" in page
    assert 'class="miss"' in page
    assert "BORDERLINE" in page


def test_write_canary_page_lands_in_the_run_dir(cards, tmp_path) -> None:
    dst = CP.write_canary_page(
        tmp_path, run_id="demo", style="clay", backend="fal", cards=cards,
        prompt="claymation",
    )
    assert dst == tmp_path / CP.PAGE_NAME and dst.is_file()
    assert dst.read_text().startswith("<!doctype html>")
