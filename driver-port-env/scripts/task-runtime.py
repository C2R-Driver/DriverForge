#!/usr/bin/env python3
"""Portable build/test interface for one exported driver task; no model or DPF controller."""

import argparse
import sys
import uuid
from pathlib import Path

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).resolve().parent / "native_experiment"),
]
from check import check, verify_inputs
from common import docker, read, write
from prepare import initialize_environment, prepare_assets, setup_assets
from team_profile import catalog

from driver_port_factory.platform.native_runner import BUILD, runtime_identity


def prepare_runtime(task, root, reuse, sdk):
    if root.exists():
        raise ValueError("Runtime already exists; build/check reuse it directly")
    kind = read(task / "task.json").get("suite_kind")
    config = catalog()[read(task / "task.json")["driver"]][1]
    if reuse:
        reuse = reuse.resolve()
        config = read(reuse / "config.json")
        environment = read(reuse / "environment.json")
        for key in ("image_id", "source_revision", "target_revision"):
            if environment[key] != config[key]:
                raise ValueError(
                    "Shared runtime differs from the pinned environment: " + key
                )
        if environment["runtime_identity"] != runtime_identity():
            raise ValueError("Shared runtime uses a different public runner")
        verify_inputs(reuse, task / "target")
        root.mkdir()
        (root / "shared").symlink_to(reuse / "shared", target_is_directory=True)
        write(root / "config.json", config)
        write(root / "environment.json", environment)
    else:
        if kind == "fixed-public-fixture":
            raise ValueError(
                "Public fixtures require --reuse of their prepared public suite"
            )
        initialize_environment(root, config)
        prepare_assets(root, task / "target", config, sdk)
    write(root / "task-binding.json", {"driver": read(task / "task.json")["driver"]})
    print("Runtime ready:", root, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "build", "check"])
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument(
        "--reuse",
        type=Path,
        help="Reuse an existing verified suite/runtime's shared assets",
    )
    parser.add_argument("--sdk", type=Path)
    args = parser.parse_args()
    task = args.task.resolve()
    root = task / ".runtime"
    if args.action == "prepare":
        prepare_runtime(
            task, root, args.reuse, args.sdk.resolve() if args.sdk else None
        )
        return
    if not (root / "environment.json").is_file():
        parser.error("Operator must run task-runtime.py prepare --task TASK first")
    target = task / "target"
    driver = read(task / "task.json")["driver"]
    verify_inputs(root, target)
    output = root / "results" / uuid.uuid4().hex[:12]
    if args.action == "build":
        setup_assets(root, target)
        result = docker(root, target, BUILD, output / "build.log", network=True)
        print(result)
        raise SystemExit(result["exit_code"])
    result = check(root, driver, target, output, "check")
    print("Complete result:", output / "check.json")
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
