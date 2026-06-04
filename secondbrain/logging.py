"""Run logging + versioning.

Every task is a "run". Each run produces:
  - a JSONL trajectory at logs/<YYYY-MM-DD>/<run_id>.jsonl (one event per line)
  - a row in the SQLite index (brain.db) for fast querying
  - an optional git commit of the trajectory file, so history is auditable
    and any run can be replayed/understood after the fact.

This is the "everything is logged and versioned" requirement.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    task        TEXT NOT NULL,
    model_key   TEXT,
    rules       TEXT,
    status      TEXT,
    summary     TEXT,
    log_path    TEXT NOT NULL,
    num_events  INTEGER DEFAULT 0,
    cost_usd    REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunLogger:
    """Writes a single run's trajectory and indexes/commits it."""

    def __init__(self, config: Config, git_commit: bool = True):
        self.config = config
        self.git_commit = git_commit
        self.run_id: Optional[str] = None
        self.log_path: Optional[Path] = None
        self._num_events = 0
        self._cost = 0.0
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.config.db_path))
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- lifecycle ---------------------------------------------------------
    def start_run(
        self,
        task: str,
        model_key: str,
        rules: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        self.run_id = datetime.now().strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
        day_dir = self.config.logs_dir / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = day_dir / f"{self.run_id}.jsonl"

        self._db.execute(
            "INSERT INTO runs (run_id, started_at, task, model_key, rules, status, log_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                self.run_id,
                _now(),
                task,
                model_key,
                rules,
                "running",
                str(self.log_path.relative_to(self.config.root)),
            ),
        )
        self._db.commit()

        self.log_event(
            "run_start",
            {"task": task, "model_key": model_key, "rules": rules, "meta": meta or {}},
        )
        return self.run_id

    def log_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.log_path is None:
            raise RuntimeError("start_run() must be called before log_event().")
        record = {"ts": _now(), "type": event_type, **payload}
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        self._num_events += 1
        if isinstance(payload.get("cost_usd"), (int, float)):
            self._cost += float(payload["cost_usd"])

    def finish_run(self, status: str = "completed", summary: str = "") -> None:
        if self.run_id is None:
            return
        self.log_event("run_finish", {"status": status, "summary": summary})
        self._db.execute(
            "UPDATE runs SET finished_at=?, status=?, summary=?, num_events=?, cost_usd=? "
            "WHERE run_id=?",
            (_now(), status, summary, self._num_events, self._cost, self.run_id),
        )
        self._db.commit()
        if self.git_commit:
            self._commit(f"log: run {self.run_id} ({status})")

    # -- versioning --------------------------------------------------------
    def _commit(self, message: str) -> None:
        if self.log_path is None:
            return
        try:
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self.config.root),
                check=True,
                capture_output=True,
            )
        except Exception:
            return  # Not a git repo; skip versioning silently.
        try:
            subprocess.run(
                ["git", "add", str(self.log_path)],
                cwd=str(self.config.root),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", message, "--", str(self.log_path)],
                cwd=str(self.config.root),
                check=True,
                capture_output=True,
            )
        except Exception:
            # Nothing staged or commit blocked: don't fail the run over logging.
            pass

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass


def list_runs(config: Config, limit: int = 20) -> list:
    """Return recent runs from the index as a list of dict rows."""
    db = sqlite3.connect(str(config.db_path))
    db.executescript(_SCHEMA)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT run_id, started_at, finished_at, task, model_key, status, "
        "num_events, cost_usd, log_path FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]
