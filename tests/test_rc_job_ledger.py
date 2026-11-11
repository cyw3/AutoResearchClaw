"""Tests for the durable job ledger (L2 execution layer)."""

from __future__ import annotations

import json

from researchclaw.execution.job_ledger import DONE, PENDING, RUNNING, JobLedger, Unit


def _unit() -> Unit:
    return Unit.new(
        "diff-0001-demo",
        "diff",
        ["checkout", "analyze", "compare"],
        idempotency_key={"repo": "a/b", "fix": "abc"},
        meta={"repo": "a/b"},
    )


def test_step_lifecycle_survives_reload(tmp_path):
    ledger = JobLedger(tmp_path)
    unit = _unit()

    assert ledger.is_done(unit, "checkout") is False
    ledger.begin(unit, "checkout")
    assert ledger.step_status(unit, "checkout") == RUNNING
    ledger.complete(unit, "checkout", {"path": "/tmp/x"})
    assert ledger.is_done(unit, "checkout") is True

    # A fresh instance must read the same state back.
    reopened = JobLedger(tmp_path)
    state = reopened.load(unit)
    assert state["steps"]["checkout"]["status"] == DONE
    assert state["steps"]["checkout"]["outputs"]["path"] == "/tmp/x"
    assert state["steps"]["checkout"]["attempts"] == 1
    assert state["steps"]["analyze"]["status"] == PENDING


def test_killed_process_is_reset_to_pending(tmp_path):
    ledger = JobLedger(tmp_path)
    unit = _unit()
    ledger.begin(unit, "analyze")

    # Simulate a process that died mid-step: rewrite the owner pid to a dead one.
    path = ledger.state_path(unit)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["steps"]["analyze"]["pid"] = 999_999
    path.write_text(json.dumps(state), encoding="utf-8")

    # The constructor performs startup recovery, so the reset is already applied
    # by the time the new ledger exists; a second pass must be idempotent.
    ledger2 = JobLedger(tmp_path)
    entry = ledger2.load(unit)["steps"]["analyze"]
    assert entry["status"] == PENDING
    assert entry["interrupted"] is True
    assert ledger2.reset_stale() == []
    # The retry counter is preserved so the caller can see the interruption.
    assert entry["attempts"] == 1


def test_completed_steps_are_not_rerun_after_restart(tmp_path):
    ledger = JobLedger(tmp_path)
    unit = _unit()
    ledger.begin(unit, "checkout")
    ledger.complete(unit, "checkout")
    ledger.begin(unit, "analyze")

    resumed = JobLedger(tmp_path)
    ran: list[str] = []
    for step in unit.steps:
        if resumed.is_done(unit, step):
            continue
        ran.append(step)
    assert ran == ["analyze", "compare"]


def test_atomic_writes_leave_no_tmp_files(tmp_path):
    ledger = JobLedger(tmp_path)
    unit = _unit()
    for step in unit.steps:
        ledger.begin(unit, step)
        ledger.complete(unit, step)
    ledger.finish(unit, DONE, {"ok": True})

    leftovers = list(tmp_path.rglob("*.tmp"))
    assert leftovers == []
    assert json.loads(ledger.state_path(unit).read_text(encoding="utf-8"))["status"] == DONE


def test_finish_records_summary_and_fail_marks_unit(tmp_path):
    ledger = JobLedger(tmp_path)
    unit = _unit()
    ledger.begin(unit, "checkout")
    ledger.fail(unit, "checkout", "git worktree add failed")
    assert ledger.load(unit)["status"] == "failed"

    ledger.finish(unit, "deferred_disk", {"free_gb": 3.0})
    state = ledger.load(unit)
    assert state["status"] == "deferred_disk"
    assert state["summary"]["free_gb"] == 3.0


def test_summary_counts_units(tmp_path):
    ledger = JobLedger(tmp_path)
    a = _unit()
    b = Unit.new("diff-0002-demo", "diff", ["checkout"])
    ledger.begin(a, "checkout")
    ledger.complete(a, "checkout")
    ledger.finish(a, DONE)
    ledger.begin(b, "checkout")

    summary = ledger.summary("diff")
    assert summary["units"] == 2
    assert summary["status_counts"][DONE] == 1
    assert summary["status_counts"][RUNNING] == 1
