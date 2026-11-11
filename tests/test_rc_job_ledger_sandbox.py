"""Tests for the ``job_ledger`` execution backend (sandbox + Stage 12 wiring)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.config import JobLedgerConfig, RCConfig
from researchclaw.experiment.job_ledger_sandbox import (
    JobLedgerSandbox,
    is_ledger_metric,
)
from researchclaw.pipeline.stage_impls._execution import _execute_experiment_run
from researchclaw.pipeline.stages import StageStatus

REPO_ROOT = Path(__file__).resolve().parents[1]

_RUNNER = """
import json
import os
from pathlib import Path

from researchclaw.execution.job_ledger import DONE, JobLedger, Unit

ledger_root = Path(os.environ["RESEARCHCLAW_LEDGER_ROOT"])
ledger = JobLedger(ledger_root)
unit = Unit.new("unit-1", "unit", ["prepare", "measure"])
for step in unit.steps:
    if ledger.is_done(unit, step):
        print(f"skip {step}", flush=True)
        continue
    ledger.begin(unit, step)
    ledger.complete(unit, step, {"n": 1})
ledger.finish(unit, DONE, {"accuracy": 0.75})
if os.environ.get("WRITE_METRICS_JSON") == "1":
    (ledger_root / "metrics.json").write_text(
        json.dumps({"metrics": {"success_rate": 0.5}}), encoding="utf-8"
    )
print("runner ok", flush=True)
"""

_FAILING_RUNNER = """
import os
from pathlib import Path

from researchclaw.execution.job_ledger import JobLedger, Unit

ledger = JobLedger(Path(os.environ["RESEARCHCLAW_LEDGER_ROOT"]))
unit = Unit.new("unit-1", "unit", ["prepare"])
ledger.begin(unit, "prepare")
ledger.fail(unit, "prepare", "boom")
print("runner failed", flush=True)
"""


def _write_runner(workdir: Path, body: str = _RUNNER) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "runner.py"
    path.write_text(body, encoding="utf-8")
    return path


def _sandbox(
    tmp_path: Path,
    *,
    timeout_sec: int = 0,
    command: tuple[str, ...] | None = None,
    extra_env: tuple[tuple[str, str], ...] = (),
    on_tick: Callable[[], None] | None = None,
) -> JobLedgerSandbox:
    workdir = tmp_path / "stage-12" / "runs" / "job"
    cmd = command or ("{python}", "runner.py", "--ledger", "{ledger_root}")
    config = JobLedgerConfig(
        command=cmd,
        timeout_sec=timeout_sec,
        env=(("PYTHONPATH", str(REPO_ROOT)),) + tuple(extra_env),
    )
    return JobLedgerSandbox(config, workdir, on_tick=on_tick)


def _config(
    tmp_path: Path,
    *,
    metric_key: str = "",
    metric_direction: str = "maximize",
    **job_ledger: object,
) -> RCConfig:
    data: dict[str, object] = {
        "project": {"name": "demo", "mode": "docs-first"},
        "research": {"topic": "job ledger topic", "domains": ["cs"]},
        "runtime": {"timezone": "UTC"},
        "notifications": {"channel": "discord"},
        "knowledge_base": {"backend": "markdown", "root": "docs/kb"},
        "openclaw_bridge": {"use_cron": True},
        "llm": {
            "provider": "openai-compatible",
            "base_url": "https://example.invalid/v1",
            "api_key_env": "OPENAI_API_KEY",
        },
        "security": {"hitl_required_stages": [5]},
        "experiment": {
            "mode": "job_ledger",
            "metric_direction": metric_direction,
            **({"metric_key": metric_key} if metric_key else {}),
            "job_ledger": dict(job_ledger),
        },
    }
    return RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


def test_build_command_substitutes_placeholders(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    cmd = sandbox.build_command()

    assert cmd[0] == sys.executable
    assert cmd[2] == "--ledger"
    assert cmd[3] == str(tmp_path / "stage-12" / "jobs" / "ledger")


def test_unknown_placeholder_fails_fast(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, command=("{python}", "{nope}"))

    with pytest.raises(ValueError, match="Unknown placeholder"):
        sandbox.build_command()


def test_explicit_ledger_root_is_resolved_relative_to_stage(tmp_path: Path) -> None:
    workdir = tmp_path / "stage-12" / "runs" / "job"
    config = JobLedgerConfig(command=("{python}", "runner.py"), ledger_root="ledger")
    sandbox = JobLedgerSandbox(config, workdir)

    assert sandbox.resolve_ledger_root() == (tmp_path / "stage-12" / "ledger").resolve()


# ---------------------------------------------------------------------------
# Sandbox runs
# ---------------------------------------------------------------------------


def test_run_collects_metrics_from_unit_summaries(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _write_runner(sandbox.workdir)

    result = sandbox.run("", timeout_sec=300)

    assert result.returncode == 0
    assert result.timed_out is False
    assert result.metrics["accuracy"] == pytest.approx(0.75)
    assert result.metrics["ledger_units_done"] == 1
    assert result.metrics["ledger_units_total"] == 1
    assert "runner ok" in result.stdout
    assert sandbox.last_summary["units"] == 1
    assert (sandbox.resolve_ledger_root() / "sandbox_summary.json").is_file()
    # The job log lives in the sandbox workdir, not in the ledger.
    assert (sandbox.workdir / "job.log").is_file()


def test_metrics_json_wins_over_unit_summaries(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, extra_env=(("WRITE_METRICS_JSON", "1"),))
    _write_runner(sandbox.workdir)

    result = sandbox.run("")

    assert result.metrics["success_rate"] == pytest.approx(0.5)
    assert "accuracy" not in result.metrics
    assert result.metrics["ledger_units_done"] == 1


_BATCH_RUNNER = """
import argparse
import os
import time
from pathlib import Path

from researchclaw.execution.job_ledger import DONE, JobLedger, Unit

parser = argparse.ArgumentParser()
parser.add_argument("--units", type=int, default=4)
parser.add_argument("--steps", type=int, default=2)
parser.add_argument("--sleep", type=float, default=0.2)
args = parser.parse_args()

ledger = JobLedger(Path(os.environ["RESEARCHCLAW_LEDGER_ROOT"]))
for i in range(args.units):
    uid = f"unit-{i}"
    unit = Unit.new(uid, "batch", [f"step-{s}" for s in range(args.steps)])
    for step in unit.steps:
        if ledger.is_done(unit, step):
            print(f"skip {uid} {step}", flush=True)
            continue
        ledger.begin(unit, step)
        time.sleep(args.sleep)
        ledger.complete(unit, step)
    ledger.finish(unit, DONE, {"score": 0.5 + i * 0.1})
"""


def test_sigkill_mid_run_resumes_from_the_ledger(tmp_path: Path) -> None:
    """The whole point of the ledger: a killed job resumes, it does not restart."""
    sandbox = _sandbox(
        tmp_path,
        command=("{python}", "runner.py", "--units", "4", "--steps", "2", "--sleep", "0.2"),
    )
    _write_runner(sandbox.workdir, _BATCH_RUNNER)
    ledger_root = sandbox.resolve_ledger_root()

    env = {
        **os.environ,
        "RESEARCHCLAW_LEDGER_ROOT": str(ledger_root),
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "runner.py", "--units", "4", "--steps", "2", "--sleep", "0.2"],
        cwd=str(sandbox.workdir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.7)
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait(timeout=10)

    partial_states = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in ledger_root.glob("batch/*.json")
    ]
    partial_done = sum(1 for s in partial_states if s.get("status") == "done")

    result = sandbox.run("")

    assert result.returncode == 0
    assert result.metrics["ledger_units_done"] == 4
    assert result.metrics["score"] == pytest.approx((0.5 + 0.6 + 0.7 + 0.8) / 4)
    if partial_done:
        assert "skip " in result.stdout

    states = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in ledger_root.glob("batch/*.json")
    ]
    attempts = [
        entry["attempts"]
        for state in states
        for entry in state["steps"].values()
    ]
    # At most the interrupted step was retried; nothing was replayed wholesale.
    assert max(attempts) <= 2
    assert sum(1 for a in attempts if a > 1) <= 1


def test_resumed_run_skips_completed_steps(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _write_runner(sandbox.workdir)
    first = sandbox.run("")
    assert first.returncode == 0

    second = sandbox.run("")
    assert second.returncode == 0
    assert "skip prepare" in second.stdout
    assert "skip measure" in second.stdout
    assert second.metrics["accuracy"] == pytest.approx(0.75)


def test_failed_units_are_reported(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _write_runner(sandbox.workdir, _FAILING_RUNNER)

    result = sandbox.run("")

    assert result.metrics["ledger_units_failed"] == 1
    assert not [key for key in result.metrics if not is_ledger_metric(key)]
    assert sandbox.last_summary["failed_units"] == ["unit-1"]


def test_configured_timeout_terminates_the_job(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path,
        timeout_sec=1,
        command=("{python}", "-c", "import time; time.sleep(30)"),
    )

    result = sandbox.run("", timeout_sec=300)

    assert result.timed_out is True
    assert result.elapsed_sec < 20


def test_liveness_hook_fires_while_the_job_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from researchclaw.experiment import job_ledger_sandbox

    monkeypatch.setattr(job_ledger_sandbox, "_TICK_SEC", 0.05)
    ticks: list[int] = []
    sandbox = _sandbox(
        tmp_path,
        command=("{python}", "-c", "import time; time.sleep(0.5)"),
        on_tick=lambda: ticks.append(1),
    )

    result = sandbox.run("")

    assert result.returncode == 0
    assert ticks, "the liveness hook must fire at least once during a long job"


def test_failing_liveness_hook_does_not_break_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from researchclaw.experiment import job_ledger_sandbox

    monkeypatch.setattr(job_ledger_sandbox, "_TICK_SEC", 0.05)

    def _boom() -> None:
        raise RuntimeError("heartbeat writer exploded")

    sandbox = _sandbox(
        tmp_path,
        command=("{python}", "-c", "import time; time.sleep(0.3)"),
        on_tick=_boom,
    )

    assert sandbox.run("").returncode == 0


# ---------------------------------------------------------------------------
# Stage 12 wiring
# ---------------------------------------------------------------------------


def test_stage12_runs_the_job_and_writes_canonical_results(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-12"
    stage_dir.mkdir(parents=True)
    _write_runner(stage_dir / "runs" / "job")

    config = _config(
        tmp_path,
        command=["{python}", "runner.py"],
        env=[["PYTHONPATH", str(REPO_ROOT)]],
        metric_key="accuracy",
        metric_direction="maximize",
    )

    result = _execute_experiment_run(
        stage_dir, run_dir, config, AdapterBundle()
    )

    assert result.status == StageStatus.DONE
    payload = json.loads((stage_dir / "runs" / "results.json").read_text("utf-8"))
    assert payload["source"] == "job_ledger"
    assert payload["primary_metric"] == pytest.approx(0.75)
    assert payload["metrics"] == {"accuracy": pytest.approx(0.75)}
    assert payload["units"] == 1
    summary = json.loads((stage_dir / "job_ledger_summary.json").read_text("utf-8"))
    assert summary["status_counts"]["done"] == 1
    run_payload = json.loads((stage_dir / "runs" / "run-1.json").read_text("utf-8"))
    assert run_payload["status"] == "completed"
    assert run_payload["metrics"] == {"accuracy": pytest.approx(0.75)}
    assert run_payload["ledger_metrics"]["ledger_units_done"] == 1


_MIXED_RUNNER = """
import os
from pathlib import Path

from researchclaw.execution.job_ledger import DONE, JobLedger, Unit

ledger = JobLedger(Path(os.environ["RESEARCHCLAW_LEDGER_ROOT"]))
ok = Unit.new("unit-1", "unit", ["prepare"])
ledger.begin(ok, "prepare")
ledger.complete(ok, "prepare")
ledger.finish(ok, DONE, {"accuracy": 0.9})

bad = Unit.new("unit-2", "unit", ["prepare"])
ledger.begin(bad, "prepare")
ledger.fail(bad, "prepare", "boom")
print("mixed done", flush=True)
"""


def test_stage12_keeps_partial_data_when_some_units_fail(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-12"
    stage_dir.mkdir(parents=True)
    _write_runner(stage_dir / "runs" / "job", _MIXED_RUNNER)

    config = _config(
        tmp_path,
        command=["{python}", "runner.py"],
        env=[["PYTHONPATH", str(REPO_ROOT)]],
        metric_key="accuracy",
    )

    result = _execute_experiment_run(stage_dir, run_dir, config, AdapterBundle())

    assert result.status == StageStatus.DONE
    payload = json.loads((stage_dir / "runs" / "results.json").read_text("utf-8"))
    assert payload["metrics"] == {"accuracy": pytest.approx(0.9)}
    assert payload["status_counts"]["failed"] == 1
    assert payload["failed_units"] == ["unit-2"]
    run_payload = json.loads((stage_dir / "runs" / "run-1.json").read_text("utf-8"))
    assert run_payload["status"] == "partial"


def test_stage12_without_command_fails_loudly(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-12"
    stage_dir.mkdir(parents=True)

    result = _execute_experiment_run(
        stage_dir, run_dir, _config(tmp_path), AdapterBundle()
    )

    assert result.status == StageStatus.FAILED
    assert "experiment.job_ledger.command" in (result.error or "")


def test_stage12_rejects_job_without_measured_metrics(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-12"
    stage_dir.mkdir(parents=True)
    _write_runner(stage_dir / "runs" / "job", _FAILING_RUNNER)

    config = _config(
        tmp_path,
        command=["{python}", "runner.py"],
        env=[["PYTHONPATH", str(REPO_ROOT)]],
    )

    result = _execute_experiment_run(stage_dir, run_dir, config, AdapterBundle())

    assert result.status == StageStatus.FAILED
    assert "zero measured metrics" in (result.error or "")
