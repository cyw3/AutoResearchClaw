"""Tests for the L2 acceptance gate script (projects/python-taint/.../verify_l2_acceptance.py).

The script is loaded by path: it lives in the project workspace, not in the
``researchclaw`` package, but its four assertions are what Phase A signs off on.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "projects"
    / "python-taint"
    / "data"
    / "pyvul"
    / "verify_l2_acceptance.py"
)


pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(),
    reason=f"L2 acceptance script not present at {SCRIPT} (projects/ is not tracked)",
)


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("verify_l2_acceptance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_unit(base: Path, unit_id: str, status: str, steps: dict | None = None) -> None:
    kind_dir = base / "job_ledger" / "diff"
    kind_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "unit_id": unit_id,
        "kind": "diff",
        "status": status,
        "steps": steps or {},
    }
    (kind_dir / f"{unit_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _passing_base(base: Path) -> Path:
    _write_unit(
        base,
        "diff-0001-demo",
        "done",
        {"analyze_parent": {"status": "done", "attempts": 2, "interrupted": True}},
    )
    _write_unit(base, "diff-0002-demo", "failed", {"db_parent": {"status": "failed"}})
    (base / "blocked_repos.txt").write_text(
        "owner/repo  # CodeQL extractor deadlock\n", encoding="utf-8"
    )
    (base / "differential_results.jsonl").write_text(
        '{"status": "zero_alert"}\n{"status": "diff_confirmed"}\n', encoding="utf-8"
    )
    return base


class TestAssertions:
    def test_all_four_pass(self, script, tmp_path: Path) -> None:
        report = script.evaluate(_passing_base(tmp_path), target_units=1, target_rows=2)
        assert report["ok"] is True
        assert report["coverage"]["pass"] and report["volume"]["pass"]
        assert report["skip_list"]["pass"] and report["resume"]["pass"]
        assert report["resume"]["evidence"][0]["step"] == "analyze_parent"

    def test_coverage_gap_fails(self, script, tmp_path: Path) -> None:
        report = script.evaluate(_passing_base(tmp_path), target_units=383, target_rows=2)
        assert report["coverage"]["pass"] is False
        assert report["coverage"]["gap"] == 382
        assert report["ok"] is False

    def test_volume_gap_fails(self, script, tmp_path: Path) -> None:
        report = script.evaluate(_passing_base(tmp_path), target_units=1, target_rows=450)
        assert report["volume"]["pass"] is False
        assert report["volume"]["rows"] == 2

    def test_missing_interrupted_step_fails_resume(self, script, tmp_path: Path) -> None:
        base = tmp_path
        _write_unit(
            base,
            "diff-0001-demo",
            "done",
            {"analyze_parent": {"status": "done", "attempts": 1}},
        )
        (base / "blocked_repos.txt").write_text("", encoding="utf-8")
        (base / "differential_results.jsonl").write_text("{}\n", encoding="utf-8")
        report = script.evaluate(base, target_units=1, target_rows=1)
        assert report["resume"]["pass"] is False
        assert report["ok"] is False

    def test_resume_can_be_waived(self, script, tmp_path: Path) -> None:
        base = tmp_path
        _write_unit(
            base, "diff-0001-demo", "done", {"analyze_parent": {"status": "done"}}
        )
        (base / "differential_results.jsonl").write_text("{}\n", encoding="utf-8")
        report = script.evaluate(
            base, target_units=1, target_rows=1, require_resume=False
        )
        assert report["resume"]["pass"] is True
        assert report["ok"] is True


class TestSkipList:
    def test_missing_reason_fails(self, script, tmp_path: Path) -> None:
        _write_unit(base := tmp_path, "diff-0001-demo", "done", {})
        (base / "blocked_repos.txt").write_text("owner/repo\n", encoding="utf-8")
        (base / "differential_results.jsonl").write_text("{}\n", encoding="utf-8")
        report = script.evaluate(base, target_units=1, target_rows=1)
        assert report["skip_list"]["pass"] is False
        assert report["skip_list"]["missing_reason"] == ["owner/repo"]

    def test_comment_lines_are_ignored(self, script, tmp_path: Path) -> None:
        _write_unit(base := tmp_path, "diff-0001-demo", "done", {})
        (base / "blocked_repos.txt").write_text(
            "# header\n\nowner/repo  # reason\n", encoding="utf-8"
        )
        (base / "differential_results.jsonl").write_text("{}\n", encoding="utf-8")
        report = script.evaluate(base, target_units=1, target_rows=1)
        assert report["skip_list"]["entries"] == 1
        assert report["skip_list"]["pass"] is True


class TestUnitAccounting:
    def test_zombie_running_is_separate_from_in_flight(
        self, script, tmp_path: Path
    ) -> None:
        base = tmp_path
        _write_unit(
            base,
            "diff-0001-never-started",
            "running",
            {"checkout_parent": {"status": "pending"}},
        )
        _write_unit(
            base,
            "diff-0002-mid-flight",
            "running",
            {"analyze_parent": {"status": "running"}},
        )
        (base / "differential_results.jsonl").write_text("{}\n", encoding="utf-8")
        report = script.evaluate(base, target_units=1, target_rows=1)
        assert report["units"]["zombie_running"] == 1
        assert report["units"]["running_in_flight"] == 1
        assert report["units"]["done"] == 0
        assert report["ok"] is False
