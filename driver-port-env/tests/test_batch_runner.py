"""Offline checks for driver concurrency and lifecycle; no model or Docker."""

import argparse
import importlib.util
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "scripts"),
    str(ROOT / "scripts/native_experiment"),
    str(ROOT / "src"),
]
spec = importlib.util.spec_from_file_location(
    "experiment_batch", ROOT / "scripts/experiment-batch.py"
)
batch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = batch
spec.loader.exec_module(batch)


def timed_worker(task):
    started = time.monotonic()
    time.sleep(0.05)
    return task, started, time.monotonic(), os.getpid()


def test_driver_concurrency_is_bounded_and_runs_each_task_once():
    starts, results = [], []
    batch.run_tasks(
        list(range(6)),
        3,
        timed_worker,
        starts.append,
        lambda task, result: results.append(result),
    )
    assert sorted(starts) == list(range(6))
    assert sorted(r[0] for r in results) == list(range(6))
    assert len({r[3] for r in results}) > 1
    events = sorted([(r[1], 1) for r in results] + [(r[2], -1) for r in results])
    active = peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    assert 1 < peak <= 3


def test_driver_lease_covers_translation_private_score_and_cleanup(
    tmp_path, monkeypatch
):
    events = []

    @contextmanager
    def reserved(*args):
        events.append("reserve")
        try:
            yield
        finally:
            events.append("release")

    def run(argv, **kwargs):
        events.append("private" if "scoring/score.py" in argv[1] else "translation")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(batch, "reserve", reserved)
    monkeypatch.setattr(batch, "selected_suite", lambda *_: tmp_path)
    monkeypatch.setattr(batch.subprocess, "run", run)
    monkeypatch.setattr(batch, "seal", lambda *a, **k: events.append("seal") or [])
    monkeypatch.setattr(batch, "clean", lambda *a: events.append("clean"))
    args = argparse.Namespace(
        batch="offline",
        method="factory",
        profile=tmp_path / "profile.json",
        output=tmp_path,
        model="fixture",
        reasoning_effort="medium",
        pricing=tmp_path / "rates.json",
        parallel=False,
        private_score=True,
    )
    result = batch.run_driver(({"factory": tmp_path}, args, "evbug"))
    assert result["state"] == "FINISHED"
    assert events == ["reserve", "translation", "seal", "private", "clean", "release"]
