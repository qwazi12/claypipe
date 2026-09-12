"""User-facing text must describe the system that exists, not the one planned.

DEFENDS: the rule that a status line is evidence, not decoration. A CLI that
reports "not built yet" about a module shipped three steps ago teaches an
operator to distrust everything else it prints.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_user_facing_text_reflects_step_3_reality() -> None:
    """DEFENDS: stale status claims in cli.py, README.md and SPEC.md."""
    cli = (REPO / "claypipe" / "cli.py").read_text()
    readme = (REPO / "README.md").read_text()
    spec = (REPO / "SPEC.md").read_text()

    assert "scoring    not built yet" not in cli, (
        "cli.py still claims scoring is unbuilt; it has shipped and runs on "
        "every paid backend"
    )
    assert "Step | 2 |" not in readme
    assert "weights.yaml is authoritative" in spec


def test_readme_status_table_has_no_shipped_step_marked_not_started() -> None:
    """DEFENDS: a README that undersells shipped work is as wrong as one that
    oversells unshipped work."""
    readme = (REPO / "README.md").read_text()
    for line in readme.splitlines():
        if line.startswith("| ") and "not started" in line:
            pytest.fail(f"README row still says 'not started': {line.strip()}")


def test_status_command_names_the_decision_that_explains_the_skip() -> None:
    """DEFENDS: an operator reading 'skipped' must be able to find out why."""
    cli = (REPO / "claypipe" / "cli.py").read_text()
    assert "skipped on dummy (D27)" in cli
    assert "**D27" in (REPO / "memory.md").read_text(), "D27 must exist to be cited"
