"""Job-ledger sandbox — runs a long-horizon, real-toolchain job.

Unlike the other backends this one does not interpret the generated Python
code.  The Python stages are still useful as a specification, but the actual
experiment is a *command* supplied by the user (``experiment.job_ledger.command``)
that is expected to be built on :mod:`researchclaw.execution.job_ledger`:

* the command takes no arguments of its own — the argv template is expanded
  with the placeholders ``{python}``, ``{run_dir}``, ``{stage_dir}``,
  ``{ledger_root}`` and ``{project_root}``;
* it writes one ledger state file per unit and may crash at any point — a
  restarted command skips the steps already recorded as ``done``;
* it reports the scientific numbers either in ``<ledger_root>/metrics.json``
  (``{"metrics": {...}}`` or a flat mapping of numbers) or in each unit's
  ``summary`` block, which this backend aggregates by mean.

Metrics therefore come from durable state on disk, not from parsing stdout.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from researchclaw.config import JobLedgerConfig
from researchclaw.execution.job_ledger import DONE, FAILED, PENDING, RUNNING, JobLedger
from researchclaw.experiment.sandbox import SandboxResult

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_TAIL_CHARS = 65536
# How often to ping the caller's liveness hook while a job runs.
_TICK_SEC = 60.0

_LEDGER_METRIC_PREFIX = "ledger_"


class JobLedgerSandbox:
    """Run the configured job command and read its results back from the ledger."""

    def __init__(
        self,
        config: JobLedgerConfig,
        workdir: Path,
        *,
        context: dict[str, str] | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.workdir = Path(workdir).resolve()
        self.context = dict(context or {})
        self.on_tick = on_tick
        self._extra_env: dict[str, str] = {}
        # Filled by run(); kept so Stage 12 can persist the bookkeeping side.
        self.last_summary: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API (matches SandboxProtocol)
    # ------------------------------------------------------------------

    def run(
        self,
        code: str,
        *,
        timeout_sec: int | None = None,
    ) -> SandboxResult:
        """Launch the job command and collect metrics from the ledger."""
        del code  # the job command, not the generated script, is the experiment
        if not self.config.command:
            raise RuntimeError(
                "job_ledger mode requires experiment.job_ledger.command "
                "(argv template, e.g. ['{python}', 'runner.py', '--ledger', "
                "'{ledger_root}'])."
            )

        self.workdir.mkdir(parents=True, exist_ok=True)
        ledger_root = self.resolve_ledger_root()
        cmd = self.build_command(ledger_root)
        effective_timeout = self._effective_timeout(timeout_sec)
        log_path = self.workdir / (self.config.log_name or "job.log")

        logger.info(
            "JobLedgerSandbox: %s (cwd=%s, ledger=%s, timeout=%s)",
            " ".join(cmd),
            self.workdir,
            ledger_root,
            "none" if effective_timeout <= 0 else f"{effective_timeout}s",
        )

        start = time.monotonic()
        returncode, timed_out = self._spawn(
            cmd, log_path, effective_timeout, ledger_root
        )
        elapsed = time.monotonic() - start

        metrics = self._collect_metrics(ledger_root)
        self.last_summary = self._summary(
            ledger_root, cmd, returncode, timed_out, elapsed
        )
        self._write_summary(ledger_root, self.last_summary)

        stdout = _read_tail(log_path)
        stderr = ""
        if returncode != 0 and not stdout:
            stderr = f"job command exited with {returncode} (see {log_path})"

        return SandboxResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            elapsed_sec=elapsed,
            metrics=metrics,
            timed_out=timed_out,
        )

    def run_project(
        self,
        project_dir: Path,
        *,
        entry_point: str = "main.py",
        timeout_sec: int = 300,
        args: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> SandboxResult:
        """Re-entry adapter for ``SandboxProtocol`` parity.

        A ledger job is a single configured command, so ``project_dir`` /
        ``entry_point`` / ``args`` do not change what runs.  ``env_overrides``
        is honoured for the duration of the run when given.
        """
        del project_dir, entry_point, args
        if env_overrides:
            self._extra_env = dict(env_overrides)
        try:
            return self.run("", timeout_sec=timeout_sec)
        finally:
            self._extra_env = {}

    # ------------------------------------------------------------------
    # Paths / command
    # ------------------------------------------------------------------

    def resolve_ledger_root(self) -> Path:
        raw = (self.config.ledger_root or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self._stage_dir() / path
            return path.resolve()
        return (self._stage_dir() / "jobs" / "ledger").resolve()

    def build_command(self, ledger_root: Path | None = None) -> list[str]:
        ledger_root = ledger_root or self.resolve_ledger_root()
        values = {
            "python": self.context.get("python") or sys.executable,
            "run_dir": self.context.get("run_dir") or str(self._run_dir()),
            "stage_dir": self.context.get("stage_dir") or str(self._stage_dir()),
            "project_root": self.context.get("project_root") or str(Path.cwd()),
            "ledger_root": str(ledger_root),
        }

        unknown: set[str] = set()

        def _substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                unknown.add(key)
                return match.group(0)
            return values[key]

        cmd = [_PLACEHOLDER_RE.sub(_substitute, str(part)) for part in self.config.command]
        if unknown:
            raise ValueError(
                "Unknown placeholder(s) in experiment.job_ledger.command: "
                f"{sorted(unknown)}; supported: {sorted(values)}"
            )
        return cmd

    def _stage_dir(self) -> Path:
        ctx = self.context.get("stage_dir")
        if ctx:
            return Path(ctx).expanduser().resolve()
        # Sandboxes are created as <stage_dir>/runs/<name>.
        parent = self.workdir.parent
        return parent.parent if parent != parent.parent else self.workdir

    def _run_dir(self) -> Path:
        ctx = self.context.get("run_dir")
        if ctx:
            return Path(ctx).expanduser().resolve()
        stage_dir = self._stage_dir()
        return stage_dir.parent if stage_dir.name.startswith("stage-") else stage_dir

    def _effective_timeout(self, requested: int | None) -> int:
        """Long jobs win over the generic experiment budget.

        ``JobLedgerConfig.timeout_sec`` is authoritative: a positive value caps
        the run, ``0`` disables the timeout entirely (the job is resumable, so
        the caller's per-attempt budget must not silently truncate it).
        """
        del requested
        return int(self.config.timeout_sec or 0)

    def _build_env(self, ledger_root: Path) -> dict[str, str]:
        env = dict(os.environ)
        for key, value in self.config.env:
            env[str(key)] = str(value)
        for key, value in self._extra_env.items():
            env[str(key)] = str(value)
        env["RESEARCHCLAW_LEDGER_ROOT"] = str(ledger_root)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    # ------------------------------------------------------------------
    # Process control
    # ------------------------------------------------------------------

    def _spawn(
        self,
        cmd: list[str],
        log_path: Path,
        timeout_sec: int,
        ledger_root: Path,
    ) -> tuple[int, bool]:
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(
                    f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
                    f"job start: {' '.join(cmd)}\n"
                )
                log.flush()
                proc = subprocess.Popen(  # noqa: S603 - argv template from config
                    cmd,
                    cwd=str(self.workdir),
                    env=self._build_env(ledger_root),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                deadline = (
                    time.monotonic() + timeout_sec if timeout_sec > 0 else None
                )
                while True:
                    slice_sec = _TICK_SEC
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return self._timed_out(proc, timeout_sec)
                        slice_sec = min(slice_sec, remaining)
                    try:
                        return proc.wait(timeout=slice_sec), False
                    except subprocess.TimeoutExpired:
                        # Long jobs must keep the pipeline heartbeat fresh for
                        # the sentinel watchdog while they run.
                        self._tick()
        except OSError as exc:
            logger.error("JobLedgerSandbox: failed to launch job command: %s", exc)
            return -1, False

    def _timed_out(
        self, proc: subprocess.Popen[Any], timeout_sec: int
    ) -> tuple[int, bool]:
        logger.warning(
            "JobLedgerSandbox: timed out after %ds — terminating job "
            "(completed ledger steps are preserved)",
            timeout_sec,
        )
        _terminate_group(proc)
        return (proc.returncode if proc.returncode is not None else -1), True

    def _tick(self) -> None:
        if self.on_tick is None:
            return
        try:
            self.on_tick()
        except Exception as exc:  # noqa: BLE001 - a heartbeat must never kill a job
            logger.debug("JobLedgerSandbox: heartbeat tick failed: %s", exc)

    # ------------------------------------------------------------------
    # Ledger reporting
    # ------------------------------------------------------------------

    def _collect_metrics(self, ledger_root: Path) -> dict[str, object]:
        metrics: dict[str, object] = {}

        explicit = ledger_root / "metrics.json"
        if explicit.is_file():
            try:
                payload = json.loads(explicit.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("JobLedgerSandbox: unreadable %s: %s", explicit, exc)
            else:
                nested = (
                    payload.get("metrics") if isinstance(payload, dict) else None
                )
                source = nested if isinstance(nested, dict) else payload
                if isinstance(source, dict):
                    metrics.update(_numeric_items(source))

        ledger = JobLedger(ledger_root)
        if not metrics:
            metrics.update(_aggregate_unit_summaries(ledger))

        counts = ledger.summary().get("status_counts", {})
        for status, key in (
            (DONE, "ledger_units_done"),
            (FAILED, "ledger_units_failed"),
            (PENDING, "ledger_units_pending"),
            (RUNNING, "ledger_units_running"),
        ):
            metrics[key] = int(counts.get(status, 0))
        metrics["ledger_units_total"] = int(sum(counts.values()))
        return metrics

    def _summary(
        self,
        ledger_root: Path,
        cmd: list[str],
        returncode: int,
        timed_out: bool,
        elapsed: float,
    ) -> dict[str, Any]:
        ledger = JobLedger(ledger_root)
        counts = ledger.summary().get("status_counts", {})
        failed_units: list[str] = []
        for state in ledger.states():
            if state.get("status") == FAILED:
                failed_units.append(str(state.get("unit_id", "?")))
        return {
            "command": cmd,
            "ledger_root": str(ledger_root),
            "returncode": returncode,
            "timed_out": timed_out,
            "elapsed_sec": round(elapsed, 3),
            "units": int(sum(counts.values())),
            "status_counts": counts,
            "failed_units": failed_units[:200],
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    def _write_summary(self, ledger_root: Path, summary: dict[str, Any]) -> None:
        path = ledger_root / "sandbox_summary.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("JobLedgerSandbox: cannot write %s: %s", path, exc)


def is_ledger_metric(key: str) -> bool:
    """True for Bookkeeping keys that are not experimental measurements."""
    return str(key).startswith(_LEDGER_METRIC_PREFIX)


def _numeric_items(payload: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in payload.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value):
            out[str(key)] = float(value)
    return out


def _aggregate_unit_summaries(ledger: JobLedger) -> dict[str, float]:
    """Mean of every numeric key found in unit ``summary`` blocks."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for state in ledger.states():
        summary = state.get("summary")
        if not isinstance(summary, dict):
            continue
        for key, value in _numeric_items(summary).items():
            sums[key] = sums.get(key, 0.0) + value
            counts[key] = counts.get(key, 0) + 1
    return {key: sums[key] / counts[key] for key in sums if counts[key]}


def _terminate_group(proc: subprocess.Popen[Any], grace_sec: float = 5.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid:
                os.killpg(pgid, sig)
            elif proc.poll() is None:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=grace_sec)
            return
        except subprocess.TimeoutExpired:
            continue


def _read_tail(path: Path, limit: int = _TAIL_CHARS) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            truncated = size > limit
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return ("...\n" + text) if truncated else text
