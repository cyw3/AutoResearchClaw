"""Durable job ledger for long-running, resumable experiment work.

One JSON state file per unit, written atomically (tmp file + ``os.replace``).
A unit declares its steps up front; the ledger records per-step status, so a
killed process can resume without re-running completed steps.

Contract: **steps must be idempotent**. The ledger only skips work, it never
rolls anything back. A step interrupted mid-flight is reset to ``pending`` on
the next start and re-executed from its beginning.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

TERMINAL_UNIT_STATUSES = {DONE, FAILED, "deferred_disk"}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class Unit:
    """A resumable unit of work with an ordered list of idempotent steps."""

    unit_id: str
    kind: str
    steps: tuple[str, ...]
    idempotency_key: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        unit_id: str,
        kind: str,
        steps: list[str] | tuple[str, ...],
        *,
        idempotency_key: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "Unit":
        return cls(
            unit_id=unit_id,
            kind=kind,
            steps=tuple(steps),
            idempotency_key={str(k): str(v) for k, v in (idempotency_key or {}).items()},
            meta=dict(meta or {}),
        )


class JobLedger:
    """File-backed, append-mostly ledger with crash recovery.

    Layout::

        <root>/heartbeat.json
        <root>/<kind>/<unit_id>.json        # unit state
        <root>/<kind>/<unit_id>/            # unit artifacts (owned by caller)
    """

    def __init__(self, root: Path | str, *, stale_after_sec: float = 900.0) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.stale_after_sec = float(stale_after_sec)
        self.heartbeat_path = self.root / "heartbeat.json"
        self.touch()
        self.reset_stale()

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def _kind_dir(self, kind: str) -> Path:
        path = self.root / kind
        path.mkdir(parents=True, exist_ok=True)
        return path

    def state_path(self, unit: Unit) -> Path:
        return self._kind_dir(unit.kind) / f"{unit.unit_id}.json"

    def unit_dir(self, unit: Unit) -> Path:
        path = self._kind_dir(unit.kind) / unit.unit_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _write_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_name(path.name + ".tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=1)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _fresh_state(self, unit: Unit) -> dict[str, Any]:
        return {
            "unit_id": unit.unit_id,
            "kind": unit.kind,
            # A materialised state file means "known", not "started": the unit
            # becomes RUNNING in begin().  Defaulting to RUNNING made every
            # never-started unit look in-flight and inflated status_counts.
            "status": PENDING,
            "idempotency_key": unit.idempotency_key,
            "meta": unit.meta,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "steps": {step: {"status": PENDING, "attempts": 0} for step in unit.steps},
        }

    def load(self, unit: Unit) -> dict[str, Any]:
        path = self.state_path(unit)
        if not path.exists():
            state = self._fresh_state(unit)
            self._write_atomic(path, state)
            return state
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A torn file must never wedge a unit: rebuild it as all-pending.
            state = self._fresh_state(unit)
            self._write_atomic(path, state)
            return state
        steps = state.setdefault("steps", {})
        for step in unit.steps:  # tolerate step lists growing between versions
            steps.setdefault(step, {"status": PENDING, "attempts": 0})
        return state

    def save(self, unit: Unit, state: dict[str, Any]) -> None:
        state["updated_at"] = _now_iso()
        self._write_atomic(self.state_path(unit), state)

    # ------------------------------------------------------------------
    # Step API
    # ------------------------------------------------------------------
    def is_done(self, unit: Unit, step: str) -> bool:
        return self.load(unit)["steps"][step]["status"] == DONE

    def step_status(self, unit: Unit, step: str) -> str:
        return self.load(unit)["steps"][step]["status"]

    def begin(self, unit: Unit, step: str) -> None:
        state = self.load(unit)
        entry = state["steps"][step]
        entry.update(
            {
                "status": RUNNING,
                "started_at": _now_iso(),
                "started_ts": time.time(),
                "pid": os.getpid(),
                "host": socket.gethostname(),
            }
        )
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        # Starting any step means the unit itself is in flight.
        state["status"] = RUNNING
        self.save(unit, state)

    def complete(
        self,
        unit: Unit,
        step: str,
        outputs: dict[str, Any] | None = None,
        note: str = "",
    ) -> None:
        state = self.load(unit)
        entry = state["steps"][step]
        entry.update({"status": DONE, "done_at": _now_iso(), "note": note})
        entry.pop("started_ts", None)
        if outputs:
            entry["outputs"] = outputs
        if state.get("status") != DONE:
            state["status"] = RUNNING
        self.save(unit, state)

    def fail(self, unit: Unit, step: str, error: str) -> None:
        state = self.load(unit)
        entry = state["steps"][step]
        entry.update({"status": FAILED, "done_at": _now_iso(), "error": error[:2000]})
        entry.pop("started_ts", None)
        state["status"] = FAILED
        self.save(unit, state)

    def finish(
        self,
        unit: Unit,
        status: str = DONE,
        summary: dict[str, Any] | None = None,
    ) -> None:
        state = self.load(unit)
        state["status"] = status
        state["finished_at"] = _now_iso()
        if summary is not None:
            state["summary"] = summary
        self.save(unit, state)

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------
    def touch(self) -> None:
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "updated_at": _now_iso(),
            "updated_ts": time.time(),
        }
        self._write_atomic(self.heartbeat_path, payload)

    def reset_stale(self) -> list[str]:
        """Reset steps left ``running`` by a dead or expired process.

        Returns the ``"<unit_id>:<step>"`` identifiers that were reset. A step is
        considered stale when its owning pid is gone, or when it has been
        running longer than ``stale_after_sec``.
        """
        reset: list[str] = []
        now = time.time()
        for path in sorted(self.root.glob("*/*.json")):
            if path.name == "heartbeat.json":
                continue
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            changed = False
            for step, entry in (state.get("steps") or {}).items():
                if entry.get("status") != RUNNING:
                    continue
                started = float(entry.get("started_ts") or 0.0)
                pid = int(entry.get("pid") or 0)
                expired = started and (now - started) > self.stale_after_sec
                if not _pid_alive(pid) or expired:
                    entry["status"] = PENDING
                    entry["interrupted"] = True
                    entry.pop("started_ts", None)
                    changed = True
                    reset.append(f"{state.get('unit_id')}:{step}")
            if changed:
                state["status"] = PENDING
                state["updated_at"] = _now_iso()
                self._write_atomic(path, state)
        return reset

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def states(self, kind: str | None = None) -> Iterator[dict[str, Any]]:
        pattern = f"{kind}/*.json" if kind else "*/*.json"
        for path in sorted(self.root.glob(pattern)):
            if path.name == "heartbeat.json":
                continue
            try:
                yield json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

    def summary(self, kind: str | None = None) -> dict[str, Any]:
        counts: dict[str, int] = {}
        step_attempts: dict[str, int] = {}
        units = 0
        for state in self.states(kind):
            units += 1
            counts[state.get("status", "?")] = counts.get(state.get("status", "?"), 0) + 1
            for step, entry in (state.get("steps") or {}).items():
                if entry.get("status") == DONE:
                    step_attempts[step] = step_attempts.get(step, 0) + int(entry.get("attempts", 1))
        return {"units": units, "status_counts": counts, "retried_steps": {
            step: attempts for step, attempts in step_attempts.items() if attempts > 0
        }}
