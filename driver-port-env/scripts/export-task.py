#!/usr/bin/env python3
"""Export pure baseline inputs with full Linux and no Factory-written worker tools."""

import argparse
import json
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/native_experiment"))
from baseline_prompt import render
from prepare import export
from team_profile import catalog


def extract(prefix, destination):
    process = subprocess.Popen(
        ["git", "-C", str(ROOT), "archive", "HEAD", prefix], stdout=subprocess.PIPE
    )
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                parts = Path(member.name).parts
                prefix_parts = Path(prefix).parts
                if member.isdir() and prefix_parts[: len(parts)] == parts:
                    continue
                if parts[: len(prefix_parts)] != prefix_parts:
                    raise ValueError("Archive path does not match the selected task")
                relative = parts[len(prefix_parts) :]
                if not relative:
                    continue
                if prefix.startswith("tasks/") and relative == ("source",):
                    continue
                member.name = str(Path(*relative))
                archive.extract(member, destination, filter="data")
                # The safe tar filter may normalize link text; retain upstream Git bytes.
                if member.issym():
                    link = destination / member.name
                    link.unlink()
                    link.symlink_to(member.linkname)
        if process.wait():
            raise RuntimeError("Committed task export failed")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "driver",
        choices=sorted(p.parent.name for p in (ROOT / "tasks").glob("*/task.json")),
    )
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--qemu", type=Path, required=True, help="Pinned QEMU source evidence"
    )
    parser.add_argument(
        "--source-repository",
        type=Path,
        help="Full pinned Linux Git repository required for the legacy source cohort",
    )
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.is_relative_to(ROOT):
        parser.error(
            "Export outside the distribution checkout so workers see only their task"
        )
    task = json.loads(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(ROOT),
                "show",
                "HEAD:tasks/" + args.driver + "/task.json",
            ],
            text=True,
        )
    )
    public = task.get("suite_kind") == "fixed-public-fixture"
    config = catalog()[args.driver][1]
    if public:
        if args.source_repository is None:
            parser.error(
                "Provide --source-repository with the complete pinned Linux tree; no cropped input"
            )
        for name in ("kernel/sched/core.c", "mm/memory.c", *task["source_entries"]):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(args.source_repository),
                    "cat-file",
                    "-e",
                    task["source_root_commit"] + ":" + name,
                ],
                check=True,
            )
    qemu = args.qemu.resolve()
    subprocess.run(
        [
            "git",
            "-C",
            str(qemu),
            "cat-file",
            "-e",
            config["qemu_revision"] + "^{commit}",
        ],
        check=True,
    )
    destination.mkdir(parents=True, exist_ok=False)
    extract("tasks/" + args.driver + "/target", destination / "target")
    # The distribution shares one Linux tree; the worker receives the complete tree.
    if public:
        export(
            args.source_repository, task["source_root_commit"], destination / "source"
        )
    else:
        extract("source/linux", destination / "source")
    (destination / "task.json").write_text(json.dumps(task, indent=2) + "\n")
    (destination / "PROMPT.md").write_text(
        render(task, destination, qemu, config, destination / ".runtime/shared")
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "add", "."], check=True)
    # Include every upstream file, even paths matched by inherited ignore rules.
    subprocess.run(["git", "-C", str(destination), "add", "-f", "source"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(destination),
            "-c",
            "user.name=Experiment preparation",
            "-c",
            "user.email=experiment@localhost",
            "commit",
            "-qm",
            "Isolated single-driver experiment input",
        ],
        check=True,
    )
    print("Exported one task with no remote or distribution Git history:", destination)
    print(
        "Operator: prepare assets using the distribution's scripts/task-runtime.py --task",
        destination,
    )
    print(
        "Worker receives source/target/prompt only; no Factory build/check or migration tools."
    )


if __name__ == "__main__":
    main()
