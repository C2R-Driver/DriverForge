"""Offline checks for queue publication and truthful producer status display."""

import importlib.util
import json
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "watch_notifications",
    Path(__file__).resolve().parents[1] / "scripts/results/watch_notifications.py",
)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def test_queued_measurement_without_start_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "REPO", tmp_path)
    folder = tmp_path / "results/batches/trial/driver"
    folder.mkdir(parents=True)
    (folder / "measurements.json").write_text(json.dumps([{"total_seconds": None}]))
    job = {"batch": "trial", "driver": "driver", "state": "QUEUED"}
    assert observer.collect({"jobs": [job]}, {"models": {}}) == []


def test_producer_blocked_is_not_displayed_as_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "REPO", tmp_path)
    folder = tmp_path / "results/batches/trial/driver"
    folder.mkdir(parents=True)
    (folder / "measurements.json").write_text(
        json.dumps([{"state": "FINISHED", "total_seconds": 60}])
    )
    job = {
        "batch": "trial",
        "driver": "driver",
        "state": "FINISHED",
        "translation_state": "PREIMPLEMENTATION_ERROR",
        "model": "model",
        "repeat": 2,
    }
    results = observer.collect({"jobs": [job]}, {"models": {}})
    assert results[0]["translation_state"] == "PREIMPLEMENTATION_ERROR"
    assert results[0]["public_passed"] is False


def test_observer_does_not_end_before_queue_starts(tmp_path, monkeypatch):
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"state": "QUEUED", "jobs": []}))
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"models": {}}))
    events = []
    monkeypatch.setattr(observer, "notify", lambda *args: events.append(args[3]))
    monkeypatch.setattr(
        observer.sys,
        "argv",
        ["observer", "--queue", str(queue), "--pricing", str(prices), "--once"],
    )
    observer.main()
    assert "observer-start" in events
    assert "queue-end" not in events


def test_notification_failure_retains_windows_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr(
        observer.subprocess, "check_output", lambda *a, **k: "C:\\logs\\run.md\n"
    )

    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(
            1, args, stderr="WSL interop connection failed"
        )

    monkeypatch.setattr(observer.subprocess, "run", fail)
    state = {"events": {}}
    observer.notify(
        state,
        tmp_path / "state.json",
        "实验",
        "driver-1",
        "驱动完成",
        "warning",
        "公开0/3",
        tmp_path / "run.md",
    )
    entry = state["events"]["driver-1"]
    assert entry["delivery"] == "failed"
    assert "WSL interop connection failed" in entry["error"]
