"""Regression tests for R13-1 zero-variance detection across conditions.

Covers the shared metric resolver in ``researchclaw.pipeline._helpers``, the
stage-14 warning emission, and the runner quality gate (which duplicates the
same check on the persisted summary).
"""

from __future__ import annotations

import json
from pathlib import Path

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.pipeline._helpers import _resolve_condition_metric_values
from researchclaw.pipeline.runner import _check_experiment_quality
from researchclaw.pipeline.stage_impls._analysis import _execute_result_analysis
from researchclaw.pipeline.stages import StageStatus


def _make_config(tmp_path: Path) -> RCConfig:
    data = {
        "project": {"name": "rc-zero-variance", "mode": "docs-first"},
        "research": {"topic": "condition wiring"},
        "runtime": {"timezone": "UTC"},
        "notifications": {"channel": "local"},
        "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
        "openclaw_bridge": {},
        "experiment": {"metric_key": "primary_metric", "metric_direction": "maximize"},
        "llm": {
            "provider": "openai-compatible",
            "base_url": "http://localhost:1234/v1",
            "api_key_env": "RC_TEST_KEY",
            "api_key": "inline",
        },
    }
    return RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)


def _write_run(run_dir: Path, metrics: dict[str, float]) -> None:
    runs_dir = run_dir / "stage-12" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": "run-1", "metrics": metrics, "stdout": ""}
    (runs_dir / "run_1.json").write_text(json.dumps(payload), encoding="utf-8")


def _ablation_warnings(stage_dir: Path) -> list[str]:
    payload = json.loads((stage_dir / "experiment_summary.json").read_text("utf-8"))
    return list(payload.get("ablation_warnings") or [])


def _run_analysis(tmp_path: Path, metrics: dict[str, float]) -> list[str]:
    config = _make_config(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stage_dir = run_dir / "stage-14"
    stage_dir.mkdir()
    _write_run(run_dir, metrics)
    result = _execute_result_analysis(
        stage_dir, run_dir, config, AdapterBundle(), llm=None
    )
    assert result.status == StageStatus.DONE
    return _ablation_warnings(stage_dir)


class TestResolveConditionMetricValues:
    def test_uses_configured_metric_key_not_insertion_order(self) -> None:
        summaries = {
            "a": {"metrics": {"loss": 0.1, "accuracy": 0.75}},
            "b": {"metrics": {"accuracy": 0.75, "loss": 0.2}},
        }
        name, values = _resolve_condition_metric_values(summaries, "accuracy")
        assert name == "accuracy"
        assert values == {"a": 0.75, "b": 0.75}

    def test_returns_nothing_when_conditions_share_no_metric(self) -> None:
        summaries = {
            "a": {"metrics": {"loss": 0.5}},
            "b": {"metrics": {"accuracy": 0.5}},
        }
        assert _resolve_condition_metric_values(summaries, "primary_metric") == (None, {})

    def test_prefers_shared_primary_metric_when_key_unset(self) -> None:
        summaries = {
            "a": {"metrics": {"primary_metric": 0.9, "primary_metric_std": 0.01}},
            "b": {"metrics": {"primary_metric_std": 0.02, "primary_metric": 0.9}},
        }
        name, values = _resolve_condition_metric_values(summaries, "")
        assert name == "primary_metric"
        assert values == {"a": 0.9, "b": 0.9}

    def test_unwraps_mean_wrapper(self) -> None:
        summaries = {
            "a": {"metrics": {"primary_metric": {"mean": 0.4, "std": 0.01}}},
            "b": {"metrics": {"primary_metric": {"mean": 0.4, "std": 0.02}}},
        }
        name, values = _resolve_condition_metric_values(summaries)
        assert name == "primary_metric"
        assert values == {"a": 0.4, "b": 0.4}

    def test_supports_legacy_flat_primary_metric(self) -> None:
        summaries = {"a": {"primary_metric": 0.3}, "b": {"primary_metric": {"mean": 0.3}}}
        name, values = _resolve_condition_metric_values(summaries)
        assert name == "primary_metric"
        assert values == {"a": 0.3, "b": 0.3}

    def test_ignores_auxiliary_metrics_in_fallback(self) -> None:
        summaries = {
            "a": {"metrics": {"success_rate": 1.0, "wall_time": 12.5}},
            "b": {"metrics": {"wall_time": 11.0, "success_rate": 1.0}},
        }
        assert _resolve_condition_metric_values(summaries, "") == (None, {})

    def test_requires_value_for_every_condition(self) -> None:
        summaries = {
            "a": {"metrics": {"primary_metric": 0.5}},
            "b": {"metrics": {"primary_metric_std": 0.01}},
        }
        assert _resolve_condition_metric_values(summaries) == (None, {})


class TestStageZeroVarianceWarning:
    def test_flags_identical_metric_despite_different_insertion_order(
        self, tmp_path: Path
    ) -> None:
        # cond_a's first metric is "loss" (0.1) while cond_b's first metric is
        # "primary_metric" (0.5): the old "first value" comparison saw 0.1 vs
        # 0.5 and missed the identical primary metric entirely.
        warnings = _run_analysis(
            tmp_path,
            {
                "cond_a/loss": 0.1,
                "cond_b/primary_metric": 0.5,
                "cond_b/loss": 0.2,
                "cond_a/primary_metric": 0.5,
            },
        )
        assert any(
            "ZERO VARIANCE" in w and "primary_metric" in w for w in warnings
        ), warnings

    def test_no_false_positive_when_only_values_coincide(self, tmp_path: Path) -> None:
        # Different metric names that happen to share a value must not be
        # reported as identical wiring.
        warnings = _run_analysis(
            tmp_path, {"cond_a/loss": 0.5, "cond_b/accuracy": 0.5}
        )
        assert not any("ZERO VARIANCE" in w for w in warnings), warnings

    def test_no_warning_when_metric_values_differ(self, tmp_path: Path) -> None:
        warnings = _run_analysis(
            tmp_path, {"cond_a/primary_metric": 0.5, "cond_b/primary_metric": 0.6}
        )
        assert not any("ZERO VARIANCE" in w for w in warnings), warnings


class TestLedgerTwoArmConditions:
    """job_ledger writes ``parent/<metric>`` / ``fix/<metric>`` in run-1.json."""

    def test_two_arms_become_two_conditions(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-14"
        stage_dir.mkdir()
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run-1.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "metrics": {
                        "parent/qualifying_alerts": 0.478261,
                        "fix/qualifying_alerts": 0.347826,
                        "units_done": 46,
                        "confirmed_rate": 0.043478,
                    },
                    "primary_metric": 0.043478,
                }
            ),
            encoding="utf-8",
        )
        result = _execute_result_analysis(
            stage_dir, run_dir, config, AdapterBundle(), llm=None
        )
        assert result.status == StageStatus.DONE
        payload = json.loads(
            (stage_dir / "experiment_summary.json").read_text(encoding="utf-8")
        )
        conditions = payload.get("condition_summaries") or {}
        assert set(conditions) == {"parent", "fix"}
        assert conditions["parent"]["metrics"]["qualifying_alerts"] == 0.478261
        # Different arms -> wiring is not broken, no zero-variance warning.
        warnings = payload.get("ablation_warnings") or []
        assert not any("ZERO VARIANCE" in w for w in warnings), warnings

    def test_identical_arms_raise_zero_variance(self, tmp_path: Path) -> None:
        warnings = _run_analysis(
            tmp_path,
            {
                "parent/qualifying_alerts": 0.4,
                "fix/qualifying_alerts": 0.4,
                "units_done": 10,
            },
        )
        assert any("ZERO VARIANCE" in w and "qualifying_alerts" in w for w in warnings)


class TestStructuredResultsConditions:
    def _run(self, tmp_path: Path, results_payload: dict[str, object]) -> dict:
        config = _make_config(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stage_dir = run_dir / "stage-14"
        stage_dir.mkdir()
        runs_dir = run_dir / "stage-12" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "results.json").write_text(
            json.dumps(results_payload), encoding="utf-8"
        )
        result = _execute_result_analysis(
            stage_dir, run_dir, config, AdapterBundle(), llm=None
        )
        assert result.status == StageStatus.DONE
        return json.loads(
            (stage_dir / "experiment_summary.json").read_text(encoding="utf-8")
        )

    def test_single_run_metrics_key_is_not_promoted_to_condition(
        self, tmp_path: Path
    ) -> None:
        # job_ledger writes a flat single-run results.json; its "metrics" dict
        # used to be promoted to a condition literally named "metrics".
        payload = self._run(
            tmp_path,
            {
                "source": "job_ledger",
                "metric_key": "confirmed_rate",
                "metrics": {
                    "units_done": 34,
                    "diff_confirmed": 4,
                    "confirmed_rate": 0.117,
                },
                "primary_metric": 0.117,
                "units": 34,
                "status_counts": {"done": 34, "failed": 2},
            },
        )
        conditions = payload.get("condition_summaries") or {}
        assert "metrics" not in conditions

    def test_condition_shaped_structured_results_are_kept(self, tmp_path: Path) -> None:
        payload = self._run(
            tmp_path,
            {
                "condition_a": {"accuracy": 0.8, "loss": 0.1},
                "condition_b": {"accuracy": 0.9, "loss": 0.2},
            },
        )
        conditions = payload.get("condition_summaries") or {}
        assert set(conditions) == {"condition_a", "condition_b"}


class TestQualityGateZeroVariance:
    def _write_summary(self, run_dir: Path, summaries: dict[str, object]) -> None:
        stage_dir = run_dir / "stage-14"
        stage_dir.mkdir(parents=True)
        payload = {
            "metrics_summary": {"cond_a/primary_metric": {"mean": 1.0}},
            "condition_summaries": summaries,
        }
        (stage_dir / "experiment_summary.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_flags_identical_primary_metric(self, tmp_path: Path) -> None:
        self._write_summary(
            tmp_path,
            {
                "a": {"metrics": {"loss": 0.1, "primary_metric": 0.5}},
                "b": {"metrics": {"primary_metric": 0.5, "loss": 0.2}},
            },
        )
        ok, message = _check_experiment_quality(tmp_path, 0)
        assert ok is False
        assert "identical" in message and "primary_metric" in message

    def test_tolerates_float_rounding(self, tmp_path: Path) -> None:
        self._write_summary(
            tmp_path,
            {
                "a": {"metrics": {"primary_metric": 0.3}},
                "b": {"metrics": {"primary_metric": 0.3 + 1e-13}},
            },
        )
        ok, message = _check_experiment_quality(tmp_path, 0)
        assert ok is False
        assert "identical" in message

    def test_resolves_non_primary_metric_without_config(self, tmp_path: Path) -> None:
        self._write_summary(
            tmp_path,
            {
                "a": {"metrics": {"rouge_l": 0.42}},
                "b": {"metrics": {"rouge_l": 0.42}},
            },
        )
        ok, message = _check_experiment_quality(tmp_path, 0)
        assert ok is False
        assert "rouge_l" in message

    def test_passes_when_metric_values_differ(self, tmp_path: Path) -> None:
        self._write_summary(
            tmp_path,
            {
                "a": {"metrics": {"primary_metric": 0.5}},
                "b": {"metrics": {"primary_metric": 0.6}},
            },
        )
        ok, message = _check_experiment_quality(tmp_path, 0)
        assert ok is True, message
