"""Flag review page tests (Build Order step 4).

The flag page decides what a human ever gets to look at, so the properties that
matter are: the selection is reproducible, an unreviewable flood is surfaced
rather than trimmed away, and a rejection leaves a record instead of a hole.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claypipe.config import load_weights
from claypipe.pipeline.score import FrameScore, Verdict, VerdictReason
from claypipe.verdi import flag_page as FP
from claypipe.verdi import loaders as L
from tests.test_canary_page import NETWORK_OFFENDERS

FIXTURES = Path(__file__).parent / "fixtures"
IMAGE = FIXTURES / "identical" / "source.png"


@pytest.fixture
def cfg():
    return load_weights().review


def score_for(index: int, f: float) -> FrameScore:
    name = f"f_{index:05d}.png"
    failing = f <= 0.75
    return FrameScore(
        frame=name, ssim=0.8, lpips_edges=0.2, identity=0.9, temporal=0.97, f=f,
        verdict=Verdict.BORDERLINE if failing else Verdict.PASS,
        reason=VerdictReason.COMPOSITE_BORDERLINE if failing else VerdictReason.ACCEPTED,
        targets_met={"ssim": not failing, "lpips_edges": True, "id": True, "tf": True},
    )


def make_population(outliers: int, passers: int) -> tuple[list[FrameScore], dict[str, Path]]:
    scores = [score_for(i, 0.50 + i * 0.0001) for i in range(outliers)]
    scores += [score_for(1000 + i, 0.90) for i in range(passers)]
    images = {s.frame: IMAGE for s in scores}
    return scores, images


def test_flag_page_selects_outliers_deterministically_with_seed(cfg, tmp_path) -> None:
    """Two runs over the same input must produce byte-identical pages.

    A review that cannot be reproduced cannot be audited or handed to a second
    reviewer, so determinism is a correctness property here, not a nicety.
    """
    scores, images = make_population(outliers=12, passers=300)

    first = FP.select_review_frames(scores, images, cfg)
    second = FP.select_review_frames(scores, images, cfg)
    assert [c.name for c in first.cards] == [c.name for c in second.cards]
    assert first.audit_total == second.audit_total == int(300 * cfg.audit_sample_fraction)

    page_a = FP.render_flag_page(run_id="r", backend="fal", selection=first, cfg=cfg)
    page_b = FP.render_flag_page(run_id="r", backend="fal", selection=second, cfg=cfg)
    assert page_a == page_b, "same input produced different bytes"

    # A different seed draws a different audit sample...
    other = FP.select_review_frames(scores, images, cfg, seed=cfg.audit_sample_seed + 1)
    audit_a = {c.name for c in first.cards if c.category == FP.CATEGORY_AUDIT}
    audit_b = {c.name for c in other.cards if c.category == FP.CATEGORY_AUDIT}
    assert audit_a != audit_b
    # ...but the outliers are never sampled, so they are identical either way.
    assert (
        [c.name for c in first.cards if c.category == FP.CATEGORY_OUTLIER]
        == [c.name for c in other.cards if c.category == FP.CATEGORY_OUTLIER]
    )


def test_flag_page_selection_is_independent_of_input_order(cfg) -> None:
    """Shuffling the scores must not change who gets reviewed."""
    scores, images = make_population(outliers=5, passers=200)
    forward = FP.select_review_frames(scores, images, cfg)
    backward = FP.select_review_frames(list(reversed(scores)), images, cfg)
    assert [c.name for c in forward.cards] == [c.name for c in backward.cards]


def test_flag_page_caps_outliers_at_200_writes_finding_to_incidents_dir(cfg, tmp_path) -> None:
    """250 outliers render 200 cards AND leave an incident with the true count.

    Trimming to a reviewable page is fine; doing it quietly is not. The dropped
    50 are the evidence that this run has a systemic problem.
    """
    scores, images = make_population(outliers=250, passers=0)
    selection = FP.select_review_frames(scores, images, cfg)

    assert selection.outlier_total == 250
    assert selection.shown == cfg.max_outlier_cards == 200
    assert selection.truncated is True

    FP.write_flag_page(
        tmp_path, run_id="demo", backend="fal", selection=selection, cfg=cfg
    )

    incidents = list((tmp_path / "incidents").glob(f"*-{FP.HIGH_OUTLIER_FINDING}.json"))
    assert len(incidents) == 1, "the flood must be recorded, not just trimmed"
    note = json.loads(incidents[0].read_text())
    assert note["breach"] == FP.HIGH_OUTLIER_FINDING
    assert note["outliers_total"] == 250
    assert note["cards_shown"] == 200
    assert note["dropped"] == 50

    page = (tmp_path / FP.PAGE_NAME).read_text()
    assert "250 frames were flagged" in page, "the page itself must admit the truncation"


def test_flag_page_shows_the_worst_frames_when_it_truncates(cfg) -> None:
    """If only some frames fit, they are the worst ones, not the first ones."""
    scores, images = make_population(outliers=250, passers=0)
    selection = FP.select_review_frames(scores, images, cfg)
    shown = [c.score.f for c in selection.cards if c.category == FP.CATEGORY_OUTLIER]
    assert shown == sorted(shown)
    assert max(shown) < 0.5 + 250 * 0.0001


def test_flag_page_reject_writes_a_rejection_note_per_frame(cfg, tmp_path) -> None:
    """A reject leaves a record; it never silently falls back to the source.

    The frame is preserved and paired with a note carrying the category, the
    reviewer's words and the score summary that justified the call.
    """
    restyled = tmp_path / "frames" / "restyled"
    restyled.mkdir(parents=True)
    for name in ("f_00007.png", "f_00009.png", "f_00011.png"):
        (restyled / name).write_bytes(IMAGE.read_bytes())

    verdicts = {
        "schema_version": 1,
        "frames": {
            "f_00007.png": {
                "category": "outlier_audit", "verdict": "reject", "note": "limbs fused",
                "score_summary": {"f": 0.62, "reason": "composite_fail", "missed": ["ssim"]},
            },
            "f_00009.png": {
                "category": "random_audit", "verdict": "reject", "note": "wrong character",
                "score_summary": {"f": 0.71, "reason": "targets_missed", "missed": ["id"]},
            },
            "f_00011.png": {
                "category": "outlier_audit", "verdict": "accept", "note": "fine",
                "score_summary": {"f": 0.74, "reason": "composite_borderline", "missed": []},
            },
        },
    }

    written = FP.apply_rejections(tmp_path, verdicts, restyled)
    assert len(written) == 2, "only rejects produce notes"

    for name, missed in (("f_00007.png", ["ssim"]), ("f_00009.png", ["id"])):
        note_path = tmp_path / FP.REJECTED_DIR / f"{name}.rejection.json"
        assert note_path.is_file()
        note = json.loads(note_path.read_text())
        assert note["frame"] == name
        assert note["category"] == verdicts["frames"][name]["category"]
        assert note["note"] == verdicts["frames"][name]["note"]
        assert note["score_summary"]["missed"] == missed
        assert note["rejected_at"].endswith("Z")
        # the pixels are preserved, not discarded
        assert (tmp_path / FP.REJECTED_DIR / name).read_bytes() == IMAGE.read_bytes()

    assert not (tmp_path / FP.REJECTED_DIR / "f_00011.png.rejection.json").exists()
    assert FP.count_human_overrides(verdicts) == 2


def test_flag_page_counts_retries_as_human_overrides(cfg) -> None:
    """qc_card.human_overrides counts every time a human contradicted the scorer."""
    verdicts = {"frames": {
        "a.png": {"verdict": "accept"}, "b.png": {"verdict": "retry"},
        "c.png": {"verdict": "reject"},
    }}
    assert FP.count_human_overrides(verdicts) == 2


def test_flag_page_is_self_contained_no_network_calls(cfg) -> None:
    """HARD: same rule as the canary page."""
    scores, images = make_population(outliers=3, passers=100)
    selection = FP.select_review_frames(scores, images, cfg)
    page = FP.render_flag_page(run_id="r", backend="fal", selection=selection, cfg=cfg)

    for offender in NETWORK_OFFENDERS:
        assert offender not in page, f"page references {offender!r}"
    for ref in re.findall(r'(?:src|href|action)\s*=\s*"([^"]*)"', page):
        assert ref.startswith("data:") or ref.startswith("#") or ref == FP.PAGE_NAME
    assert "<link" not in page and "<script" not in page


def test_flag_page_offers_only_the_three_human_choices(cfg) -> None:
    """HUMAN_QUEUE is set by the budget logic, never chosen by a reviewer."""
    scores, images = make_population(outliers=2, passers=0)
    selection = FP.select_review_frames(scores, images, cfg)
    page = FP.render_flag_page(run_id="r", backend="fal", selection=selection, cfg=cfg)

    offered = set(re.findall(r'name="frame:[^"]+:verdict" value="([a-z_]+)"', page))
    assert offered == {"accept", "retry", "reject"}
    assert "human_queue" not in page.lower()


def test_flag_page_form_round_trips_the_review_contract(cfg) -> None:
    """The saved review records what the REVIEWER SAW, from hidden score fields."""
    body = (
        "schema_version=1&run_id=demo&"
        "frame:f_00007.png:category=outlier_audit&frame:f_00007.png:verdict=retry&"
        "frame:f_00007.png:note=try+again&frame:f_00007.png:f=0.62&"
        "frame:f_00007.png:reason=composite_fail&frame:f_00007.png:missed=ssim"
    )
    parsed = L.review_verdicts_from_form(body)
    entry = parsed["frames"]["f_00007.png"]
    assert entry["category"] == "outlier_audit"
    assert entry["verdict"] == "retry"
    assert entry["note"] == "try again"
    assert entry["score_summary"] == {"f": 0.62, "reason": "composite_fail", "missed": ["ssim"]}
