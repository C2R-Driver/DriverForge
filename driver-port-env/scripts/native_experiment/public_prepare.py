"""Prepare two complete-source public-fixture inputs, reusing the existing runtime assets."""

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import HERE, git, read, sha, write
from experiment_config import load_config
from prepare import audit_seed, export, root_commit

from driver_port_factory.platform.native_runner import runtime_identity


def runtime_profile(task, config=None):
    config = config or load_config(
        HERE.parents[1] / "configs/experiments/common-runtime.json"
    )
    return {
        "qemu": config["qemu"],
        "ready_text": "# ",
        "qemu_args": [
            "-machine",
            config["machine"] + ",kernel-irqchip=split",
            "-accel",
            config["accelerator"],
            "-cpu",
            config["cpu"],
            "-smp",
            str(config["cpus"]),
            "-m",
            config["memory"],
            "-no-reboot",
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-boot",
            "d",
            "-bios",
            config["firmware"],
            "-device",
            "isa-debug-exit,iobase=0xf4,iosize=0x04",
        ]
        + task.get("runtime_qemu_args", []),
    }


def freeze_tests(root, config, assets):
    index = {}
    for driver, task in config["drivers"].items():
        destination = root / "shared/public-tests" / driver
        shutil.copytree(assets / driver, destination)
        rows = read(destination / "cases.json")
        if [row["id"] for row in rows] != [*task["tests"], *task["benchmarks"]]:
            raise ValueError(
                "Configured cases differ from the existing fixed assertions"
            )
        profile = runtime_profile(task, config)
        prefix = driver + "/" if task.get("case_namespace") else ""
        for row in rows:
            index[prefix + row["id"]] = {"profile": profile, "case": row["case"]}
        index[prefix + task["boot_case"]] = {
            "profile": profile,
            "case": {"timeout_seconds": 120, "steps": [{"wait_serial": "# "}]},
        }
    write(root / "shared/public-tests/index.json", index)


def prepare(args):
    config = load_config(args.config)
    reuse = args.reuse.resolve()
    environment = read(reuse / "environment.json")
    if environment["image_id"] != config["image_id"]:
        raise ValueError("Reuse must use the same frozen official Docker image")
    for key in ("accelerator", "machine", "cpus", "memory", "firmware"):
        if environment[key] != config[key]:
            raise ValueError("Shared runtime assets differ from common " + key)
    for relative, key in (
        ("shared/sdk/cargo-osdk", "sdk_reuse_sha256"),
        ("shared/initramfs.cpio.gz", "initramfs_sha256"),
    ):
        if sha(reuse / relative) != config[key]:
            raise ValueError("Shared runtime asset differs: " + relative)
    for repository, revision in (
        (args.source, config["source_revision"]),
        (args.target, config["target_revision"]),
        (args.qemu, config["qemu_revision"]),
    ):
        git(repository, "cat-file", "-e", revision + "^{commit}")
    # Prove the selected source commit has the complete kernel context, not an extracted driver.
    for relative in (
        "Makefile",
        "kernel/sched/core.c",
        "mm/memory.c",
        "drivers/input/input.c",
    ):
        git(args.source, "cat-file", "-e", config["source_revision"] + ":" + relative)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "shared").mkdir()
    # Hard links reuse bytes while staying valid under Docker's isolated /dpf-shared mount.
    for name in ("sdk", "disks"):
        shutil.copytree(
            reuse / "shared" / name, root / "shared" / name, copy_function=os.link
        )
    os.link(reuse / "shared/initramfs.cpio.gz", root / "shared/initramfs.cpio.gz")
    write(root / "config.json", config)
    write(
        root / "operator/ground-truth.json",
        {
            "commit": config["target_revision"],
            "files": {name: {} for name in config["drivers"]},
            "note": "Drivers already absent upstream; no translated candidate is an input.",
        },
    )
    freeze_tests(root, config, args.assets)
    environment.update(
        source_revision=config["source_revision"],
        target_revision=config["target_revision"],
        qemu_evidence_revision=config["qemu_revision"],
        runtime_identity=runtime_identity(),
        suite_kind=config["suite_kind"],
        kernel_log_level=config["kernel_log_level"],
        control_status=config["control_status"],
        public_runtime_sha256=sha(HERE / "public_runtime.py"),
        public_fixture_files={
            str(p.relative_to(root)): sha(p)
            for p in (root / "shared/public-tests").rglob("*")
            if p.is_file()
        },
    )
    environment["runtime_collectors"] = {
        script: sha(HERE / script)
        for script in {
            t.get("runtime_script", "runtime.py") for t in config["drivers"].values()
        }
    }
    write(root / "environment.json", environment)
    for driver, task in config["drivers"].items():
        publish(root, driver, task, config, args.source, args.target)
    print(
        "Prepared fixed-public-fixture inputs; no model, kernel build or QEMU check:",
        root,
    )


def publish(root, driver, task, config, source, target):
    seed = root / "seeds" / driver
    tree = seed / "target"
    export(target, config["target_revision"], tree)
    if (tree / task["omit"]).exists():
        raise ValueError(
            "Expected driver-absent upstream target; refusing a candidate input"
        )
    # Retain the established interactive shell profile used by the common native suite.
    manifest = tree / "OSDK.toml"
    manifest.write_text(
        manifest.read_text().replace('init_args = ["sh", "-l"]', "init_args = []")
    )
    assets = root / "shared/public-tests" / driver
    fixture = tree / task["fixture_file"]
    shutil.copy2(assets / task["fixture_asset"], fixture)
    interface = tree / ".dpf-output/harness/public/INTERFACE.md"
    interface.parent.mkdir(parents=True)
    shutil.copy2(assets / "INTERFACE.md", interface)
    write(
        tree / ".native-task.json",
        {"driver": driver, "upstream": config["target_revision"]},
    )
    commit = root_commit(tree, force=True)
    (seed / "source").symlink_to(source.resolve(), target_is_directory=True)
    write(
        seed / "task.json",
        dict(
            task,
            driver=driver,
            target_root_commit=commit,
            source_root_commit=config["source_revision"],
            source_scope="complete-linux-tree",
            upstream_linux=config["source_revision"],
            upstream_asterinas=config["target_revision"],
            environment=read(root / "environment.json"),
        ),
    )
    expected = (
        read(root / "shared/oracle-sources.json")
        if (root / "shared/oracle-sources.json").exists()
        else {}
    )
    for path in (manifest, tree / "tools/qemu_args.sh"):
        expected[str(path.relative_to(tree))] = sha(path)
    write(root / "shared/oracle-sources.json", expected)
    audit_seed(root, driver)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "target", "qemu", "reuse"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=HERE.parents[1] / "configs/experiments/public-drivers.json",
    )
    parser.add_argument(
        "--assets",
        type=Path,
        default=HERE.parents[1] / "src/driver_port_factory/data/public-tests",
    )
    prepare(parser.parse_args())


if __name__ == "__main__":
    main()
