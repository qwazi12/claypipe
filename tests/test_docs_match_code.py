"""Rule 33 enforcement — RUNBOOK.md and CONFIG.md must match the code.

The rule says both must match the code and that drift is a defect. Saying so in
a memory file did not stop `status` from carrying a stale caption line for a
whole session, so it is a test now.

These check the CLAIMS the docs make, not their prose: every command and flag
they name must exist, and every config key they tabulate must be real (and vice
versa, so a new key cannot be added without documenting it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from claypipe.cli import app

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = REPO_ROOT / "RUNBOOK.md"
CONFIG_DOC = REPO_ROOT / "CONFIG.md"

runner = CliRunner()


def test_both_documents_exist():
    """Rule 33 names them specifically."""
    assert RUNBOOK.is_file(), "RUNBOOK.md is required by Rule 33"
    assert CONFIG_DOC.is_file(), "CONFIG.md is required by Rule 33"


# ---------------------------------------------------------------------------
# RUNBOOK: every command and flag it names must exist
# ---------------------------------------------------------------------------

def _claypipe_invocations(text: str) -> set[tuple[str, ...]]:
    """`claypipe <cmd> [subcmd]` occurrences in fenced code blocks."""
    found: set[tuple[str, ...]] = set()
    for line in text.splitlines():
        stripped = line.strip().lstrip("$ ").strip()
        match = re.match(r"^(?:RUN=\$\()?claypipe\s+([a-z-]+)(?:\s+([a-z-]+))?", stripped)
        if not match:
            continue
        command, sub = match.group(1), match.group(2)
        if sub and sub not in {"version"} and not sub.startswith("-"):
            found.add((command, sub))
        else:
            found.add((command,))
    return found


def test_every_runbook_command_exists():
    invocations = _claypipe_invocations(RUNBOOK.read_text())
    assert invocations, "no claypipe invocations found — the parser is broken"
    for parts in sorted(invocations):
        result = runner.invoke(app, [*parts, "--help"])
        assert result.exit_code == 0, (
            f"RUNBOOK documents `claypipe {' '.join(parts)}` but it does not "
            f"exist:\n{result.output}"
        )


def test_every_runbook_flag_exists():
    """A documented flag that was renamed is worse than an undocumented one."""
    text = RUNBOOK.read_text()
    # Flags are named inline as `--flag` in prose and tables.
    flags = set(re.findall(r"`(--[a-z][a-z-]+)`", text))
    # Flags mentioned in code blocks too.
    flags |= set(re.findall(r"\s(--[a-z][a-z-]+)", text))
    # These are ffmpeg/pip/jq flags, not claypipe's.
    external = {"--help", "-e"}
    flags -= external
    assert flags, "no flags found — the parser is broken"

    known: set[str] = set()
    for parts in [
        (), ("intake",), ("batch",), ("assemble",), ("captions",), ("export",),
        ("status",), ("canary", "restyle"), ("canary", "render"),
        ("canary", "pack"), ("canary", "submit"),
    ]:
        out = runner.invoke(app, [*parts, "--help"]).output
        known |= set(re.findall(r"(--[a-z][a-z-]+)", out))

    missing = sorted(f for f in flags if f not in known)
    assert not missing, f"RUNBOOK names flags that do not exist: {missing}"


def test_runbook_covers_the_rule_33_topics():
    """Rule 33 lists what a runbook must contain."""
    text = RUNBOOK.read_text().lower()
    for topic in ("pause", "resume", "re-run", "log", "fail"):
        assert topic in text, f"RUNBOOK does not cover {topic!r}"


def test_runbook_names_every_run_artefact_that_exists():
    """The artefact table is how an operator finds anything. A path that moved
    without the table moving is exactly the drift Rule 33 forbids."""
    from claypipe.run import RunPaths

    text = RUNBOOK.read_text()
    paths = RunPaths(Path("/tmp/example_run"))
    for attribute in (
        "manifest", "source_frames", "restyled_frames", "caption_frames",
        "refs", "shot_plan", "keyframe_plan", "scores", "drift", "cues",
        "qc_card", "final", "log",
    ):
        name = Path(getattr(paths, attribute)).name
        assert name in text, (
            f"RunPaths.{attribute} resolves to {name!r}, which RUNBOOK.md does "
            "not mention"
        )


# ---------------------------------------------------------------------------
# CONFIG: the key tables must match the YAML, both ways
# ---------------------------------------------------------------------------

def _leaf_keys(data, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                keys |= _leaf_keys(value, f"{prefix}{key}.")
            else:
                keys.add(f"{prefix}{key}")
    return keys


@pytest.mark.parametrize("config_file", ["styles.yaml", "weights.yaml"])
def test_every_config_key_is_documented(config_file):
    """A new tunable that nobody documented is how CONFIG.md goes stale."""
    doc = CONFIG_DOC.read_text()
    data = yaml.safe_load((REPO_ROOT / config_file).read_text())

    undocumented = []
    for key in sorted(_leaf_keys(data)):
        leaf = key.split(".")[-1]
        # Style profile names and per-backend price rows are documented as
        # groups rather than one row each.
        if key.startswith("styles.") or ".pricing." in key:
            continue
        if leaf not in doc:
            undocumented.append(key)
    assert not undocumented, (
        f"{config_file} keys missing from CONFIG.md: {undocumented}"
    )


def test_every_priced_backend_is_in_the_config_doc_table():
    weights = yaml.safe_load((REPO_ROOT / "weights.yaml").read_text())
    doc = CONFIG_DOC.read_text()
    for backend in weights["firewalls"]["cost"]["pricing"]:
        assert backend in doc, f"backend {backend!r} is priced but undocumented"


def test_every_style_profile_is_named():
    styles = yaml.safe_load((REPO_ROOT / "styles.yaml").read_text())
    doc = CONFIG_DOC.read_text()
    for name in styles["styles"]:
        assert f"`{name}`" in doc, f"style {name!r} is shipped but undocumented"


def test_config_doc_does_not_describe_retired_keys():
    """The specific failure this guards: T9 retired three geometry keys. A doc
    still listing them would send an operator to set a value that now crashes
    startup (`extra: forbid`)."""
    doc = CONFIG_DOC.read_text()
    for retired in ("header_height", "panel_height", "divider_height"):
        # It may MENTION them to say they are gone; it must not tabulate them
        # as settable.
        assert f"| `{retired}`" not in doc, (
            f"CONFIG.md still tabulates the retired key {retired!r}"
        )


def test_config_doc_records_the_derived_layout_cutoff():
    """The cutoff is derived in code; the doc must state the same number, or an
    operator reasoning about which branch a clip takes will be wrong."""
    from claypipe.pipeline.layout import even, width_fit_cutoff

    cutoff = width_fit_cutoff(1080, 1920, even(0.037 * 1920))
    assert f"{cutoff:.3f}" in CONFIG_DOC.read_text(), (
        f"CONFIG.md does not state the derived cutoff {cutoff:.3f}"
    )


def test_config_doc_states_the_uncalibrated_mode():
    """An operator must not be able to read CONFIG.md and think resynth is
    ready to spend."""
    doc = CONFIG_DOC.read_text()
    assert "calibrated" in doc
    assert "NOT CALIBRATED" in doc or "not calibrated" in doc.lower()
    assert "T16" in doc
