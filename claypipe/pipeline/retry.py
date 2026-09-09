"""Stage: retry policy and cost firewalls (SPEC §4, brief §7).

Five hard circuit-breakers. None of them is a warning; every one halts the run
and writes an incident note a human has to read:

  1. CANARY GATE    batch refuses to run without an approved canary verdict.
  2. RETRY BUDGET   max 2 retries per frame, and total retries capped at 15% of
                    frame count. A clip needing more than that has a systemic
                    prompt problem, not frame problems.
  3. KILL SWITCH    after the first 50 frames, running mean F < 0.70 aborts.
  4. RUN SPEND CAP  cumulative run spend above --max-cost-usd halts.
  5. PROJECT CAP    cumulative spend across ALL runs (SPEC Addendum A3): the
                    per-run cap alone does not stop ten aborted runs at $19
                    each costing $190.

Every API call is authorised against the caps and written to the ledger BEFORE
it executes, then reconciled with the actual cost afterwards. An unauthorised
call is impossible by construction: `authorize()` is what returns the entry
id that `reconcile()` needs.

Retry actions follow the verdict REASON, not just the verdict (Decision 1):
a frame flagged for a missed component target and one flagged for a sagging
composite are the same verdict with different diagnoses.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from ..config import FirewallConfig, WeightsConfig
from ..logging import RunLogger, utc_now
from ..run import RunPaths
from .score import FrameScore, Verdict, VerdictReason

CANARY_VERDICT_NAME = "canary_verdict.json"
RUN_LEDGER_NAME = "spend_ledger.jsonl"
PROJECT_LEDGER_NAME = "project_spend_ledger.jsonl"
INCIDENTS_DIR = "incidents"


class Breach(str, Enum):
    """Why a run was halted. Recorded in the incident note."""

    CANARY_MISSING = "canary_missing"
    CANARY_NOT_APPROVED = "canary_not_approved"
    RETRY_BUDGET_EXCEEDED = "retry_budget_exceeded"
    KILL_SWITCH_MEAN_F = "kill_switch_mean_f"
    SPEND_CAP_RUN = "spend_cap_run"
    SPEND_CAP_PROJECT = "spend_cap_project"


class RunHalted(RuntimeError):
    """A firewall tripped. The run stops here and waits for a human."""

    def __init__(self, breach: Breach, message: str, incident: Path | None = None) -> None:
        super().__init__(message)
        self.breach = breach
        self.incident = incident


class CanaryGateError(RunHalted):
    """Batch was asked to run without an approved canary verdict."""


def write_incident(
    paths: RunPaths, breach: Breach | str, detail: dict, logger: RunLogger | None = None
) -> Path:
    """An incident note is the whole point of a pause: a human must be able to
    see what tripped, on what evidence, without re-running anything."""
    directory = paths.root / INCIDENTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    label = breach.value if isinstance(breach, Breach) else str(breach)
    note = {
        "schema_version": 1,
        "written_at": utc_now(),
        "breach": label,
        "run_dir": str(paths.root),
        **detail,
    }
    path = directory / f"{utc_now().replace(':', '').replace('.', '')}-{label}.json"
    path.write_text(json.dumps(note, indent=2) + "\n")
    if logger is not None:
        logger.error("firewall.breach", breach=label, incident=str(path), **detail)
    return path


# --------------------------------------------------------------------------
# Firewall 1 — the canary gate
# --------------------------------------------------------------------------

def require_canary_approval(paths: RunPaths, logger: RunLogger | None = None) -> dict:
    """Batch refuses to run without `canary_verdict.json` saying approved: true.

    No verdict -> no spend. There is deliberately no override flag: an escape
    hatch here would defeat the only gate standing between a bad prompt and a
    full batch of paid calls.
    """
    verdict_path = paths.root / CANARY_VERDICT_NAME

    if not verdict_path.is_file():
        raise CanaryGateError(
            Breach.CANARY_MISSING,
            f"canary gate: {CANARY_VERDICT_NAME} not found in {paths.root}. "
            "Batch cannot start before a human has reviewed the canary frames. "
            "Run `claypipe canary <run>` and approve the result.",
        )
    try:
        verdict = json.loads(verdict_path.read_text())
    except json.JSONDecodeError as exc:
        raise CanaryGateError(
            Breach.CANARY_MISSING,
            f"canary gate: {verdict_path} is not valid JSON ({exc}). Refusing to "
            "treat an unreadable verdict as approval.",
        ) from exc

    if verdict.get("approved") is not True:
        raise CanaryGateError(
            Breach.CANARY_NOT_APPROVED,
            f"canary gate: {verdict_path} does not say approved: true "
            f"(found {verdict.get('approved')!r}). Batch will not start.",
        )
    if logger is not None:
        logger.info("firewall.canary.approved", verdict_path=str(verdict_path))
    return verdict


# --------------------------------------------------------------------------
# Firewalls 4 and 5 — the spend ledger
# --------------------------------------------------------------------------

@dataclass
class SpendLedger:
    """Append-only spend record for one run, plus the project-wide roll-up.

    Caps are checked in `authorize()`, i.e. BEFORE the call goes out. Checking
    after would mean discovering the overrun by paying for it.
    """

    paths: RunPaths
    project_dir: Path
    cfg: FirewallConfig
    run_id: str
    logger: RunLogger | None = None
    max_cost_usd_run: float | None = None
    _entries: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_cost_usd_run is None:
            self.max_cost_usd_run = self.cfg.cost.max_cost_usd_run

    @property
    def run_ledger(self) -> Path:
        return self.paths.root / RUN_LEDGER_NAME

    @property
    def project_ledger(self) -> Path:
        return self.project_dir / PROJECT_LEDGER_NAME

    def _append(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    @staticmethod
    def _total(path: Path) -> float:
        """Sum a ledger: the reconciled actual wins, the estimate stands in
        until it arrives, so an in-flight call is never counted as free."""
        if not path.is_file():
            return 0.0
        charged: dict[str, float] = {}
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            entry_id = rec.get("entry_id")
            if entry_id is None:
                continue
            if rec["event"] == "authorized":
                charged[entry_id] = float(rec["estimated_usd"])
            elif rec["event"] == "reconciled":
                charged[entry_id] = float(rec["actual_usd"])
        return round(sum(charged.values()), 6)

    def run_total(self) -> float:
        return self._total(self.run_ledger)

    def project_total(self) -> float:
        return self._total(self.project_ledger)

    def authorize(self, *, frame: str, backend: str, stage: str) -> str:
        """Check both caps, then record the intent to spend. Returns entry id.

        Raises RunHalted BEFORE any money is committed if the projected total
        would breach a cap.
        """
        estimate = self.cfg.cost.per_call(backend)
        projected_run = round(self.run_total() + estimate, 6)
        projected_project = round(self.project_total() + estimate, 6)

        if self.max_cost_usd_run is not None and projected_run > self.max_cost_usd_run:
            incident = write_incident(
                self.paths, Breach.SPEND_CAP_RUN,
                {
                    "frame": frame, "stage": stage, "backend": backend,
                    "spent_usd": self.run_total(), "next_call_usd": estimate,
                    "projected_usd": projected_run, "cap_usd": self.max_cost_usd_run,
                },
                self.logger,
            )
            raise RunHalted(
                Breach.SPEND_CAP_RUN,
                f"run spend cap reached: ${self.run_total():.4f} spent, next call "
                f"${estimate:.4f} would reach ${projected_run:.4f} against a cap of "
                f"${self.max_cost_usd_run:.4f}. Halted before the call.",
                incident,
            )

        project_cap = self.cfg.cost.max_cost_usd_project
        if project_cap is not None and projected_project > project_cap:
            incident = write_incident(
                self.paths, Breach.SPEND_CAP_PROJECT,
                {
                    "frame": frame, "stage": stage, "backend": backend,
                    "project_spent_usd": self.project_total(), "next_call_usd": estimate,
                    "projected_usd": projected_project, "cap_usd": project_cap,
                },
                self.logger,
            )
            raise RunHalted(
                Breach.SPEND_CAP_PROJECT,
                f"PROJECT spend cap reached: ${self.project_total():.4f} spent across "
                f"all runs, next call ${estimate:.4f} would reach "
                f"${projected_project:.4f} against a cap of ${project_cap:.4f}. "
                "Halted before the call.",
                incident,
            )

        entry_id = uuid.uuid4().hex[:12]
        record = {
            "entry_id": entry_id, "event": "authorized", "at": utc_now(),
            "run_id": self.run_id, "frame": frame, "stage": stage,
            "backend": backend, "estimated_usd": estimate,
        }
        self._entries[entry_id] = record
        self._append(self.run_ledger, record)
        self._append(self.project_ledger, record)
        if self.logger is not None:
            self.logger.info(
                "spend.authorized", entry_id=entry_id, frame=frame, stage=stage,
                estimated_usd=estimate, run_total_usd=projected_run,
            )
        return entry_id

    def reconcile(self, entry_id: str, actual_usd: float) -> None:
        """Record what the call actually cost, once it is known."""
        if entry_id not in self._entries:
            raise ValueError(f"unknown ledger entry {entry_id!r}; authorize() first")
        record = {
            "entry_id": entry_id, "event": "reconciled", "at": utc_now(),
            "run_id": self.run_id, "actual_usd": float(actual_usd),
            "estimated_usd": self._entries[entry_id]["estimated_usd"],
        }
        self._append(self.run_ledger, record)
        self._append(self.project_ledger, record)


# --------------------------------------------------------------------------
# Firewalls 2 and 3 — retry policy, budget, kill switch
# --------------------------------------------------------------------------

class RetryAction(str, Enum):
    ACCEPT = "accept"
    RETRY_LOWER_STRENGTH = "retry_lower_strength"
    RETRY_NEW_SEED = "retry_new_seed"
    HUMAN_QUEUE = "human_queue"


@dataclass(frozen=True)
class RetryDecision:
    frame: str
    action: RetryAction
    attempt: int
    verdict: Verdict
    reason: VerdictReason
    strength: float | None = None
    seed: int | None = None
    note: str = ""

    @property
    def is_retry(self) -> bool:
        return self.action in (RetryAction.RETRY_LOWER_STRENGTH, RetryAction.RETRY_NEW_SEED)


@dataclass
class RetryController:
    """Decides what happens to each scored frame, and enforces the budgets.

    Retries are counted globally as well as per frame, because the per-frame cap
    alone cannot see a clip that is failing everywhere at once.
    """

    cfg: WeightsConfig
    paths: RunPaths
    frame_count: int
    base_strength: float
    logger: RunLogger | None = None
    _attempts: dict[str, int] = field(default_factory=dict)
    _total_retries: int = 0
    _f_values: list[float] = field(default_factory=list)

    @property
    def firewalls(self) -> FirewallConfig:
        return self.cfg.firewalls

    @property
    def total_retries(self) -> int:
        return self._total_retries

    @property
    def retry_budget(self) -> int:
        """Absolute retry allowance for this clip."""
        return int(self.frame_count * self.firewalls.total_retry_budget_fraction)

    def attempts_for(self, frame: str) -> int:
        return self._attempts.get(frame, 0)

    def mean_f(self) -> float:
        return sum(self._f_values) / len(self._f_values) if self._f_values else 0.0

    # -- firewall 3 -------------------------------------------------------
    def observe(self, score: FrameScore) -> None:
        """Record a frame's F for the kill switch, then check it.

        Called once per distinct frame outcome. Raises RunHalted when the
        running mean has gone bad, so a doomed run stops at frame 50 rather
        than frame 700.
        """
        self._f_values.append(score.f)
        ks = self.firewalls.kill_switch
        if len(self._f_values) < ks.after_frames:
            return
        mean = self.mean_f()
        if mean < ks.min_mean_f:
            incident = write_incident(
                self.paths, Breach.KILL_SWITCH_MEAN_F,
                {
                    "frames_scored": len(self._f_values), "mean_f": round(mean, 4),
                    "min_mean_f": ks.min_mean_f, "after_frames": ks.after_frames,
                    "last_frame": score.frame,
                },
                self.logger,
            )
            raise RunHalted(
                Breach.KILL_SWITCH_MEAN_F,
                f"kill switch: mean F is {mean:.3f} after {len(self._f_values)} frames, "
                f"below the {ks.min_mean_f} floor. Aborting rather than spending "
                "the rest of the clip on a run that is already bad.",
                incident,
            )

    # -- firewall 2 -------------------------------------------------------
    def _spend_retry(self, frame: str, score: FrameScore) -> None:
        """Consume one retry from both budgets, or halt."""
        budget = self.retry_budget
        if self._total_retries + 1 > budget:
            incident = write_incident(
                self.paths, Breach.RETRY_BUDGET_EXCEEDED,
                {
                    "frame": frame, "total_retries": self._total_retries,
                    "budget": budget, "frame_count": self.frame_count,
                    "fraction": self.firewalls.total_retry_budget_fraction,
                    "verdict": score.verdict.value, "reason": score.reason.value,
                },
                self.logger,
            )
            raise RunHalted(
                Breach.RETRY_BUDGET_EXCEEDED,
                f"retry budget exhausted: {self._total_retries} retries against a "
                f"budget of {budget} "
                f"({self.firewalls.total_retry_budget_fraction:.0%} of "
                f"{self.frame_count} frames). A clip needing this many retries has "
                "a systemic prompt problem, not frame problems. Run paused for a human.",
                incident,
            )
        self._total_retries += 1
        self._attempts[frame] = self.attempts_for(frame) + 1

    def decide(self, score: FrameScore, *, base_seed: int) -> RetryDecision:
        """What to do with a scored frame.

        Branches on the verdict REASON, not the verdict alone (Decision 1):
          targets_missed / composite_borderline -> one retry at strength -0.10
          composite_fail                        -> retry with a new seed
        Either path goes to the human queue once the per-frame cap is spent.
        """
        frame = score.frame
        attempts = self.attempts_for(frame)
        cap = self.firewalls.max_retries_per_frame

        if score.verdict is Verdict.PASS:
            return RetryDecision(
                frame=frame, action=RetryAction.ACCEPT, attempt=attempts,
                verdict=score.verdict, reason=score.reason, note="auto-accepted",
            )

        if attempts >= cap:
            decision = RetryDecision(
                frame=frame, action=RetryAction.HUMAN_QUEUE, attempt=attempts,
                verdict=score.verdict, reason=score.reason,
                note=f"per-frame retry cap of {cap} reached; escalated to human review",
            )
            if self.logger is not None:
                self.logger.warn(
                    "retry.escalated", frame=frame, attempts=attempts,
                    verdict=score.verdict.value, reason=score.reason.value,
                )
            return decision

        self._spend_retry(frame, score)
        attempt = self.attempts_for(frame)

        if score.reason is VerdictReason.COMPOSITE_FAIL:
            # A FAIL is not a near miss; nudging the strength will not rescue it.
            # A new seed gives the backend a genuinely different starting point.
            decision = RetryDecision(
                frame=frame, action=RetryAction.RETRY_NEW_SEED, attempt=attempt,
                verdict=score.verdict, reason=score.reason,
                seed=base_seed + attempt * 10_000,
                strength=self.base_strength,
                note=f"F={score.f:.3f} below the fail line; retrying with a new seed",
            )
        else:
            delta = self.firewalls.borderline_strength_delta
            decision = RetryDecision(
                frame=frame, action=RetryAction.RETRY_LOWER_STRENGTH, attempt=attempt,
                verdict=score.verdict, reason=score.reason,
                strength=round(max(0.05, self.base_strength + delta * attempt), 4),
                seed=base_seed,
                note=(
                    f"missed {','.join(score.missed_targets)}"
                    if score.reason is VerdictReason.TARGETS_MISSED
                    else f"F={score.f:.3f} below the pass line"
                ),
            )

        if self.logger is not None:
            self.logger.info(
                "retry.scheduled", frame=frame, action=decision.action.value,
                attempt=attempt, reason=score.reason.value,
                strength=decision.strength, seed=decision.seed,
                total_retries=self._total_retries, budget=self.retry_budget,
            )
        return decision

    def summary(self) -> dict:
        return {
            "frames_scored": len(self._f_values),
            "mean_f": round(self.mean_f(), 4) if self._f_values else None,
            "total_retries": self._total_retries,
            "retry_budget": self.retry_budget,
            "frames_retried": len(self._attempts),
        }
