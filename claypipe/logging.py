"""Structured JSON logging with a runId on every line (Rules 23, 34).

Every log line carries timestamp/level/service/runId so a run can be
reconstructed from `runs/<run_id>/logs/run.jsonl` alone. Secrets are never
logged — callers pass metadata, never payloads or headers (Rule 6).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVICE = "claypipe"


def utc_now() -> str:
    """UTC, ISO-8601, always (Rule 38)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunLogger:
    """Append-only JSONL log for one run, mirrored to stderr."""

    def __init__(self, run_id: str, log_path: Path | None = None, echo: bool = True) -> None:
        self.run_id = run_id
        self.log_path = log_path
        self.echo = echo
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, level: str, event: str, **fields: Any) -> None:
        record = {
            "timestamp": utc_now(),
            "level": level,
            "service": SERVICE,
            "runId": self.run_id,
            "event": event,
            **fields,
        }
        line = json.dumps(record, default=str)
        if self.log_path is not None:
            with self.log_path.open("a") as fh:
                fh.write(line + "\n")
        if self.echo:
            print(line, file=sys.stderr, flush=True)

    def info(self, event: str, **fields: Any) -> None:
        self.log("INFO", event, **fields)

    def warn(self, event: str, **fields: Any) -> None:
        self.log("WARN", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.log("ERROR", event, **fields)
