"""Offline timeout checks: a real local process tree, no model or Docker."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import experiment_deadline as deadline


def test_timeout_stops_detached_child_and_records_limit(tmp_path, monkeypatch):
    directory = tmp_path / "comparisons/trial/fixture/dpf"
    directory.mkdir(parents=True)
    (directory.parent / "protocol.json").write_text(
        json.dumps({"factory": str(tmp_path)})
    )
    (directory / "run.json").write_text(json.dumps({"state": "RUNNING"}))
    marker = tmp_path / "child.pid"
    child = "import time; time.sleep(60)"
    parent = (
        "import subprocess,sys,time; from pathlib import Path; "
        f'p=subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
        f"Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    monkeypatch.setattr(deadline.subprocess, "check_output", lambda *a, **k: "")
    collected = []
    monkeypatch.setitem(
        sys.modules,
        "comparison",
        SimpleNamespace(
            configure_factory=lambda _: None,
            collect=lambda path, method: collected.append(method),
        ),
    )
    with (tmp_path / "log").open("w") as log:
        process, timed_out = deadline.run_translation(
            [sys.executable, "-c", parent],
            log,
            0.5,
            tmp_path,
            "fixture",
            "trial",
            ("dpf",),
        )
    assert timed_out and process.poll() is not None
    pid = int(marker.read_text())
    path = Path(f"/proc/{pid}/stat")
    assert not path.exists() or path.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    record = json.loads((directory / "run.json").read_text())
    assert record["state"] == record["stop_reason"] == "TIME_LIMIT"
    assert record["limit_seconds"] == 0.5
    assert collected == ["dpf"]


def test_normal_exit_does_not_become_timeout(tmp_path):
    with (tmp_path / "log").open("w") as log:
        result, timed_out = deadline.run_translation(
            [sys.executable, "-c", "pass"],
            log,
            5,
            tmp_path,
            "fixture",
            "trial",
            ("dpf",),
        )
    assert result.returncode == 0 and not timed_out
