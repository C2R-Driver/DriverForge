"""Operator wall-clock deadline, including detached workers and task containers."""

import json
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


def stop_tree(process):
    processes = {}
    for path in Path("/proc").glob("[0-9]*"):
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            processes[int(path.name)] = int(fields[1])
        except (OSError, ValueError):
            pass
    descendants = {process.pid}
    while True:
        found = {pid for pid, parent in processes.items() if parent in descendants}
        if found <= descendants:
            break
        descendants |= found
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in sorted(descendants, reverse=True):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(1)
    process.wait(timeout=5)


def run_translation(argv, log, seconds, suite, driver, trial, methods):
    if seconds is None:
        return subprocess.run(
            argv, stdout=log, stderr=subprocess.STDOUT, check=False
        ), False
    started = time.monotonic()
    process = subprocess.Popen(
        argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
    )
    try:
        process.wait(timeout=seconds)
        return process, False
    except subprocess.TimeoutExpired:
        stop_tree(process)
        works = [suite / "trials" / method / driver / trial for method in methods]
        ids = subprocess.check_output(["docker", "ps", "-q"], text=True).split()
        if ids:
            containers = json.loads(
                subprocess.check_output(["docker", "inspect", *ids], text=True)
            )
            for container in containers:
                if any(
                    Path(m["Source"]).is_relative_to(work)
                    for m in container.get("Mounts", [])
                    if m.get("Source")
                    for work in works
                ):
                    subprocess.run(
                        ["docker", "stop", "-t", "3", container["Id"]],
                        check=True,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
        from comparison import collect, configure_factory

        protocol_path = suite / "comparisons" / trial / driver / "protocol.json"
        protocol = json.loads(protocol_path.read_text())
        configure_factory(protocol["factory"])
        for method in methods:
            directory = protocol_path.parent / method
            path = directory / "run.json"
            record = json.loads(path.read_text()) if path.exists() else {}
            record.update(
                state="TIME_LIMIT",
                stop_reason="TIME_LIMIT",
                ended_at=datetime.now(UTC).isoformat(),
                total_seconds=time.monotonic() - started,
                limit_seconds=seconds,
                exit_code=process.returncode,
            )
            path.write_text(json.dumps(record, indent=2) + "\n")
            collect(protocol_path, method)
        return process, True
