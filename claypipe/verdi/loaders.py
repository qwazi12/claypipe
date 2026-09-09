"""Reading what a human decided (SPEC §5).

The pipeline polls for a verdict file rather than serving a page and waiting on
a request, because there is no server. These loaders are the contract between
Step 4's pages and Step 5's `cli.batch`.

Nothing here is forgiving. A verdict that cannot be parsed, or that fails its
schema, raises: an unreadable decision must never be interpreted as approval,
and a review file that lost half its entries to a parse error must never look
like a review where half the frames were fine.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, Field, ValidationError, model_validator

from ..config import ConfigError
from ..logging import utc_now
from . import FIELD_SEP

CANARY_VERDICT_NAME = "canary_verdict.json"
REVIEW_VERDICT_NAME = "review_verdicts.json"
PROMPT_OVERRIDE_NAME = "prompt_override.txt"

Approved = Literal[True, False, "adjust"]


class NoCanaryVerdict(FileNotFoundError):
    """No verdict file where one was expected."""


class CanaryTimeout(TimeoutError):
    """Waited for a human and gave up."""


class CanaryRejected(RuntimeError):
    """The human said no. Carries their reason, unedited."""

    def __init__(self, reason: str, verdict: dict) -> None:
        super().__init__(reason)
        self.reason = reason
        self.verdict = verdict


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class FrameVerdict(BaseModel):
    model_config = {"extra": "allow"}

    verdict: str
    note: str = ""


class CanaryVerdict(BaseModel):
    model_config = {"extra": "allow"}

    schema_version: int = 1
    decided_at: str = ""
    decider: str = "operator"
    approved: Approved
    reason: str = ""
    prompt_override: str = ""
    frames: dict[str, FrameVerdict] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _required_fields(self) -> "CanaryVerdict":
        if self.approved is False and not self.reason.strip():
            raise ValueError(
                "a rejected canary must carry a reason: the operator who reads "
                "this later needs to know what was wrong with the prompt"
            )
        if self.approved == "adjust" and not self.prompt_override.strip():
            raise ValueError(
                "an 'adjust' canary must carry a prompt_override — that is the "
                "entire point of choosing adjust over reject"
            )
        return self


class ReviewVerdicts(BaseModel):
    model_config = {"extra": "allow"}

    schema_version: int = 1
    decided_at: str = ""
    frames: dict[str, dict] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def read_canary_verdict(path: Path) -> dict:
    """Load and validate `canary_verdict.json`.

    Raises NoCanaryVerdict when absent, ConfigError when malformed. There is no
    third outcome where a broken file is treated as a yes.
    """
    if not path.is_file():
        raise NoCanaryVerdict(f"no canary verdict at {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON ({exc}). Refusing to treat an unreadable "
            "verdict as a decision."
        ) from exc
    try:
        return CanaryVerdict.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise ConfigError(f"{path} failed the canary verdict schema:\n{exc}") from exc


def poll_canary_verdict(
    path: Path,
    *,
    timeout_s: int = 600,
    poll_s: float = 1.0,
    sleep=time.sleep,
    now=time.monotonic,
) -> dict:
    """Block until a human writes a decision, then return it.

    `sleep` and `now` are injectable so this is testable without spending real
    seconds — a test that waits ten minutes is a test nobody runs.
    """
    if not path.parent.is_dir():
        raise NoCanaryVerdict(
            f"run directory {path.parent} does not exist; nothing will ever "
            "write a verdict there"
        )
    deadline = now() + timeout_s
    while True:
        if path.is_file():
            try:
                return read_canary_verdict(path)
            except ConfigError:
                raise
        if now() >= deadline:
            raise CanaryTimeout(
                f"waited {timeout_s}s for {path.name} and it never appeared. "
                "Batch will not start without an approved canary."
            )
        sleep(poll_s)


def require_approved_canary(path: Path, **kwargs) -> dict:
    """Poll, then insist on approval.

    Surfaces the operator's reason verbatim on rejection — the point of asking
    for a reason is that somebody reads it.
    """
    verdict = poll_canary_verdict(path, **kwargs)
    if verdict["approved"] is False:
        raise CanaryRejected(verdict.get("reason", ""), verdict)
    return verdict


def load_flag_verdicts(path: Path) -> dict:
    """Load `review_verdicts.json`. A missing file means no review yet: {}.

    A file that exists but cannot be parsed is an error, never an empty review —
    silently dropping entries would turn a lost review into a clean bill of health.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON ({exc}). Refusing to report a corrupt "
            "review as an empty one."
        ) from exc
    try:
        loaded = ReviewVerdicts.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} failed the review verdict schema:\n{exc}") from exc

    if len(loaded.frames) != len(raw.get("frames", {})):
        raise ConfigError(
            f"{path}: {len(raw.get('frames', {}))} frames on disk but "
            f"{len(loaded.frames)} survived parsing. Refusing to drop entries."
        )
    return loaded.model_dump()


# --------------------------------------------------------------------------
# Form submissions
# --------------------------------------------------------------------------

def parse_form_submission(body: str) -> dict:
    """Turn a page's form-encoded submission into a verdict dict.

    Accepts either a bare query string or the whole URL the browser landed on,
    because what an operator actually has to hand is the address bar.

    Nested fields arrive as `frame:<name>:<field>` — frame names contain "."
    but never ":", so the separator is unambiguous.
    """
    query = urlsplit(body).query or body
    pairs = parse_qsl(query, keep_blank_values=True)

    flat: dict[str, Any] = {}
    frames: dict[str, dict[str, str]] = {}
    for key, value in pairs:
        if key.startswith(f"frame{FIELD_SEP}"):
            _, name, field = key.split(FIELD_SEP, 2)
            frames.setdefault(name, {})[field] = value
        else:
            flat[key] = value

    out: dict[str, Any] = {
        "schema_version": int(flat.get("schema_version", 1)),
        "decided_at": flat.get("decided_at") or utc_now(),
        "frames": frames,
    }
    for key in ("decider", "reason", "prompt_override", "run_id"):
        if key in flat:
            out[key] = flat[key]
    if "approved" in flat:
        out["approved"] = {"true": True, "false": False}.get(
            flat["approved"], flat["approved"]
        )
    return out


def canary_verdict_from_form(body: str) -> dict:
    """Parse a canary submission and validate it against the contract."""
    parsed = parse_form_submission(body)
    try:
        return CanaryVerdict.model_validate(parsed).model_dump()
    except ValidationError as exc:
        raise ConfigError(f"canary submission failed its schema:\n{exc}") from exc


def review_verdicts_from_form(body: str) -> dict:
    """Parse a flag-page submission into the review contract.

    The page carries each card's score summary in hidden fields, so the saved
    review records WHAT THE REVIEWER SAW, not what the scorer says later.
    """
    parsed = parse_form_submission(body)
    frames: dict[str, dict] = {}
    for name, entry in parsed.get("frames", {}).items():
        missed = [m for m in entry.get("missed", "").split(",") if m]
        raw_f = entry.get("f", "")
        try:
            f_value = float(raw_f)
        except (TypeError, ValueError):
            f_value = None
        frames[name] = {
            "category": entry.get("category", ""),
            "verdict": entry.get("verdict", ""),
            "note": entry.get("note", ""),
            "score_summary": {
                "f": f_value,
                "reason": entry.get("reason", "") or None,
                "missed": missed,
            },
        }
    return {
        "schema_version": parsed["schema_version"],
        "decided_at": parsed["decided_at"],
        "frames": frames,
    }


# --------------------------------------------------------------------------
# Prompt override
# --------------------------------------------------------------------------

def write_canary_verdict(run_dir: Path, verdict: dict) -> Path:
    """Write `canary_verdict.json` ATOMICALLY (temp file, then rename).

    The gate polls this path. A half-written file caught mid-read would either
    fail to parse or, worse, parse into something the operator never decided, so
    the rename is what makes the file appear whole or not at all.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    final = run_dir / CANARY_VERDICT_NAME
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
    tmp.replace(final)  # atomic within a filesystem
    return final


def merge_prompt_override(run_manifest_path: Path, verdict: dict) -> Path | None:
    """Record an 'adjust' verdict's revised prompt WITHOUT overwriting anything.

    The override lands in its own file and the manifest gains a POINTER to it.
    The original style prompt lives in `styles.yaml` and is never touched, so a
    per-run adjustment can never silently become a project-wide edit.
    """
    if verdict.get("approved") != "adjust":
        return None
    override = (verdict.get("prompt_override") or "").strip()
    if not override:
        return None
    if not run_manifest_path.is_file():
        raise NoCanaryVerdict(f"no run manifest at {run_manifest_path}")

    run_dir = run_manifest_path.parent
    override_path = run_dir / PROMPT_OVERRIDE_NAME
    override_path.write_text(override + "\n")

    manifest = json.loads(run_manifest_path.read_text())
    manifest["prompt_override_source"] = str(override_path.name)
    run_manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return override_path
