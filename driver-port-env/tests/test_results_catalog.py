"""Offline tests for duplicate snapshots and missing research measurements."""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "results_catalog",
    Path(__file__).resolve().parents[1] / "scripts/results/catalog.py",
)
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


def measurement(root, directory, rows):
    path = root / "results/batches" / directory / "measurements.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))


def row(**changes):
    return {
        "model": "model",
        "reasoning_effort": "medium",
        "driver": "driver",
        "method": "dpf",
        "trial": "sample-alpha",
        **changes,
    }


def test_parent_and_nested_snapshots_are_one_run_and_conflicts_remain_visible(tmp_path):
    measurement(tmp_path, "batch", [row(cost_estimate=1, cases_passed=0)])
    measurement(tmp_path, "batch/trial", [row(usd_estimate=2, cases_passed=3)])
    result = catalog.write_catalog(tmp_path, tmp_path / "catalog")
    assert result["run_count"] == 1
    assert result["snapshot_count"] == 2
    run = result["runs"][0]
    assert run["usd_estimate"] is None
    assert set(run["conflicting_fields"]) == {"usd_estimate", "cases_passed"}
    assert run["method"] == "factory"
    assert len(run["snapshots"]) == 2


def test_failures_distinct_trials_and_unknown_cost_are_kept(tmp_path):
    measurement(
        tmp_path,
        "batch",
        [
            row(state="FAILED", suite_passed=False),
            row(trial="sample-beta", state="FINISHED", suite_passed=True, usd_estimate=0),
        ],
    )
    runs = catalog.group_runs(catalog.collect(tmp_path))
    assert len(runs) == 2
    assert {run["suite_passed"] for run in runs} == {True, False}
    assert {run["usd_estimate"] for run in runs} == {None, 0}


def test_no_trial_is_inferred_not_claimed_independent_and_usage_is_preserved(tmp_path):
    measurement(tmp_path, "batch", [row(trial=None, usage={"input_tokens": 7})])
    run = catalog.group_runs(catalog.collect(tmp_path))[0]
    assert run["trial"] is None
    assert run["identity_inferred"] is True
    assert run["input_tokens"] == 7
    assert run["build_passed"] is None
