"""Tests for the M6/A3 number lineage ledger (stage 17 → stage 22 audit)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from researchclaw.config import RCConfig
from researchclaw.lineage import LINEAGE_FILENAME, LineageLedger, hash_file

SUMMARY = {
    "best_run": {
        "metrics": {
            "parent/qualifying_alerts": 0.478261,
            "fix/qualifying_alerts": 0.347826,
            "units_done": 46,
        }
    },
    "condition_summaries": {
        "parent": {"metrics": {"qualifying_alerts": 0.478261}},
        "fix": {"metrics": {"qualifying_alerts": 0.347826}},
    },
    "metrics_summary": {
        "parent/qualifying_alerts": {
            "min": 0.1,
            "max": 1.0,
            "mean": 0.478261,
            "count": 46,
        }
    },
}

RESULTS_TABLE = r"""
\begin{table}[h]
\centering
\caption{Detection results}
\begin{tabular}{lc}
\hline
Method & Miss rate \\
\hline
Ours & 87.3 \\
\hline
\end{tabular}
\end{table}
"""

BACKED_TABLE = r"""
\begin{table}[h]
\centering
\caption{Detection results}
\begin{tabular}{lc}
\hline
Arm & Qualifying alert rate \\
\hline
parent & 0.478261 \\
\hline
\end{tabular}
\end{table}
"""


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "rc-test-run"
    (run_dir / "stage-12" / "runs").mkdir(parents=True)
    (run_dir / "stage-14").mkdir()
    (run_dir / "stage-14" / "experiment_summary.json").write_text(
        json.dumps(SUMMARY, indent=2), encoding="utf-8"
    )
    (run_dir / "stage-12" / "runs" / "run-1.json").write_text(
        json.dumps({"metrics": SUMMARY["best_run"]["metrics"]}, indent=2),
        encoding="utf-8",
    )
    return run_dir


class TestBuild:
    def test_best_run_entries_point_at_stage12_artifact(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path)
        ledger = LineageLedger.from_run_dir(run_dir)
        assert len(ledger) > 0

        raw = run_dir / "stage-12" / "runs" / "run-1.json"
        entry = next(
            e for e in ledger if e.metric_key == "parent/qualifying_alerts"
            and e.claim.startswith("best_run.metrics")
        )
        assert entry.source_stage == 12
        assert entry.artifact.startswith("stage-12/runs/run-1.json#")
        assert entry.hash == hash_file(raw)
        assert entry.run_id == "rc-test-run"
        assert entry.cited_norms == ("R2",)

    def test_condition_entries_point_at_summary(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path)
        ledger = LineageLedger.from_run_dir(run_dir)
        entry = next(
            e for e in ledger
            if e.claim == "condition_summaries.fix.metrics.qualifying_alerts"
        )
        assert entry.source_stage == 14
        assert entry.artifact.startswith("stage-14/experiment_summary.json#")
        assert entry.value == pytest.approx(0.347826)

    def test_metric_summary_stats_are_recorded(self, tmp_path: Path) -> None:
        ledger = LineageLedger.from_run_dir(_run_dir(tmp_path))
        claims = {e.claim for e in ledger}
        assert "metrics_summary.parent/qualifying_alerts.mean" in claims

    def test_empty_run_dir_yields_empty_ledger(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "empty"
        run_dir.mkdir()
        assert len(LineageLedger.from_run_dir(run_dir)) == 0


class TestPersistence:
    def test_jsonl_roundtrip(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path)
        ledger = LineageLedger.from_run_dir(run_dir)
        path = ledger.write(run_dir / LINEAGE_FILENAME)

        reloaded = LineageLedger.load(path)
        assert len(reloaded) == len(ledger)
        assert reloaded.entries[0].to_dict() == ledger.entries[0].to_dict()

    def test_load_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert len(LineageLedger.load(tmp_path / "nope.jsonl")) == 0


class TestImmutability:
    def test_check_hashes_flags_rewritten_summary(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path)
        ledger = LineageLedger.from_run_dir(run_dir)
        assert ledger.check_hashes(run_dir) == []

        # Simulate a REFINE rerun rewriting the summary in place.
        summary_path = run_dir / "stage-14" / "experiment_summary.json"
        summary_path.write_text('{"best_run": {"metrics": {}}}', encoding="utf-8")

        stale = ledger.check_hashes(run_dir)
        assert stale
        assert any("hash mismatch" in item for item in stale)

    def test_check_hashes_flags_missing_artifact(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path)
        ledger = LineageLedger.from_run_dir(run_dir)
        (run_dir / "stage-12" / "runs" / "run-1.json").unlink()
        assert any("artifact missing" in s for s in ledger.check_hashes(run_dir))


class TestVerification:
    def test_rejects_table_number_without_lineage(self, tmp_path: Path) -> None:
        """Phase A acceptance #2: an unbacked table number must not land."""
        ledger = LineageLedger.from_run_dir(_run_dir(tmp_path))
        result = ledger.verify(RESULTS_TABLE)
        assert result.severity == "REJECT"
        assert result.passed is False
        assert any(u.in_table for u in result.unverified_numbers)

    def test_accepts_backed_table_number(self, tmp_path: Path) -> None:
        ledger = LineageLedger.from_run_dir(_run_dir(tmp_path))
        result = ledger.verify(BACKED_TABLE)
        assert result.severity != "REJECT", result.summary

    def test_provenance_returns_artifact_reference(self, tmp_path: Path) -> None:
        ledger = LineageLedger.from_run_dir(_run_dir(tmp_path))
        entry = ledger.provenance(0.478261)
        assert entry is not None
        assert "experiment_summary.json" in entry.artifact or "run-1.json" in entry.artifact
        assert entry.hash.startswith("sha256:")


def _config_data(tmp_path: Path, experiment: dict | None = None) -> dict:
    data = {
        "project": {"name": "lineage-test", "mode": "docs-first"},
        "research": {"topic": "provenance"},
        "runtime": {"timezone": "UTC"},
        "notifications": {"channel": "local"},
        "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
        "llm": {
            "provider": "openai-compatible",
            "base_url": "http://localhost:1234/v1",
            "api_key_env": "RC_TEST_KEY",
            "api_key": "inline",
        },
    }
    if experiment is not None:
        data["experiment"] = experiment
    return data


class TestConfigFlag:
    def test_lineage_enforce_defaults_false(self, tmp_path: Path) -> None:
        cfg = RCConfig.from_dict(
            _config_data(tmp_path), project_root=tmp_path, check_paths=False
        )
        assert cfg.experiment.lineage_enforce is False

    def test_lineage_enforce_can_be_enabled(self, tmp_path: Path) -> None:
        cfg = RCConfig.from_dict(
            _config_data(tmp_path, {"mode": "job_ledger", "lineage_enforce": True}),
            project_root=tmp_path,
            check_paths=False,
        )
        assert cfg.experiment.lineage_enforce is True
