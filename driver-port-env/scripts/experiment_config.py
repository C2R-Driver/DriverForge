"""Shared runtime settings; archived suites keep their own frozen manifests."""

import json
from pathlib import Path


def load_config(path):
    path = Path(path)
    config = json.loads(path.read_text())
    if config.get("runtime_profile"):
        common = json.loads((path.parent / config["runtime_profile"]).read_text())
        for key, value in common.items():
            if key in config and config[key] != value:
                raise ValueError("Task suite differs from shared runtime: " + key)
        config = common | config
    return config


def network_required(task):
    return task.get("network_required", False) or any(
        case.startswith("iperf3/") for case in task.get("benchmarks", [])
    )
